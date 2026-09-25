"""言われたことを、どのエージェントの、どの仕事に振るかを決める（振り分け係）。

エージェントの名刺からスキルの一覧を集めて、軽いモデル（Haiku）に選ばせる。エージェントが増えても、
名刺を読むだけなので、ここのコードは変えなくてよい（docs/architecture.md の「振り分けと A2A」）。

判定に使うのは短い分類なので、道具も会話の続きも要らない。うまく選べなかったときは、
そのドメインの自由質問の窓口（`ask`）に回す（docs/architecture.md）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from kei_agent import runner
from kei_agent.config import Config
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve, resolve_selected
from kei_agent.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

# 判定の上限時間。モデルは provider 別 recipe から解決する。
TIMEOUT_MINUTES = 2
ASK = "ask"
# 「どのエージェントでもない（本体が自分で答える）」を選ばせるための名前
SELF = "self"
STATUS_TEXT = "どこに聞くか選んでいる…"
# 判定に渡す一言の長さ（長文は先頭だけで足りる）
TEXT_LIMIT = 600
_JSON = re.compile(r"\{.*?\}", re.DOTALL)

PROMPT = """次の「言われたこと」が、どの仕事に当たるかを1つ選んでください。

仕事の一覧:
{skills}

言われたこと:
{text}

JSON 1行だけで答えてください。ほかの文は書かないでください。
形: {{"skill": "<仕事の id>", "days": <日数。要らなければ入れない>}}
どれにも当てはまらない、または迷うときは {{"skill": "{ask}"}} にしてください。"""


@dataclass
class Choice:
    """振り分けの結果。`agent` が空なら、本体（研究の claude）が自分で答える。"""
    agent: str = ""
    skill: str = ASK
    params: dict = field(default_factory=dict)


def catalog(skills: list[dict]) -> str:
    """名刺のスキルを、判定用の短い一覧にする。"""
    lines = []
    for skill in skills:
        name = skill.get("id") or ""
        if not name:
            continue
        about = (skill.get("description") or skill.get("name") or "").splitlines()[0][:160]
        lines.append(f"- {name}: {about}")
    return "\n".join(lines)


def json_object(text: str) -> dict | None:
    """モデルの返事から、最初の {...} を JSON として拾う（前後に文が付いていてもよい）。"""
    found = _JSON.search(text or "")
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse(text: str, allowed: set[str]) -> Choice:
    """返ってきた文から JSON を拾う。読めなければ ask に回す。

    エージェントをまたぐときは、仕事の名前が `course:list-due` のように「相手:仕事」になる。
    """
    data = json_object(text)
    if data is None:
        return Choice()
    name = str(data.get("skill") or "")
    if name not in allowed:
        return Choice()
    agent, _, skill = name.rpartition(":")
    params = {}
    if isinstance(data.get("days"), int) and 1 <= data["days"] <= 400:
        params["days"] = data["days"]
    return Choice(agent=agent, skill=skill or ASK, params=params)


def workspace(config: Config, actor: str = "router") -> Workspace:
    """判定だけを動かす場所（何も書かないが、claude は作業場を要る）。

    Codex は作業場に担当 agent の skill を置くので、分類は actor ごとに別の場所にする
    （research と course の分類が同時に走っても、同じ skill の置き場を取り合わない）。
    """
    cwd = config.state_dir / "router" if actor == "router" else config.state_dir / "classifier" / actor
    cwd.mkdir(parents=True, exist_ok=True)
    return Workspace("router", ChannelKind.COURSE, cwd, timeout_minutes=TIMEOUT_MINUTES,
                     # 研究用のシステムプロンプトは要らない（分類だけなので、短いものに差し替える）
                     system_prompt=config.repo_root / "prompts" / "router.md")


async def pick(config: Config, skills: list[dict], text: str, *, store=None) -> Choice:
    """どの仕事かを選ぶ（相手が1人のとき）。選べなければ `ask`。"""
    allowed = {str(s.get("id")) for s in skills if s.get("id")}
    if not allowed:
        return Choice()
    return await _choose(config, catalog(skills), allowed, text, ASK, store=store)


async def pick_across(config: Config, by_agent: dict[str, list[dict]], text: str, *, store=None) -> Choice:
    """どのエージェントの、どの仕事かを選ぶ。どれでもなければ、本体が自分で答える（agent が空）。"""
    lines, allowed = [], {SELF}
    for agent, skills in by_agent.items():
        for skill in skills:
            name = str(skill.get("id") or "")
            if not name:
                continue
            about = (skill.get("description") or skill.get("name") or "").splitlines()[0][:160]
            allowed.add(f"{agent}:{name}")
            lines.append(f"- {agent}:{name}: {about}")
    if len(allowed) == 1:
        return Choice()
    lines.append(f"- {SELF}: 上のどれでもないとき（研究全体の相談、雑談、考えごと）")
    return await _choose(config, "\n".join(lines), allowed, text, SELF, store=store)


async def _choose(config: Config, skills: str, allowed: set[str], text: str, fallback: str, *, store=None) -> Choice:
    prompt = PROMPT.format(skills=skills, text=(text or "").strip()[:TEXT_LIMIT], ask=fallback)
    try:
        recipe = (resolve_selected(config, store, "router", UseCase.ROUTING)
                  if store is not None else resolve("router", config.agent_profiles["router"].provider,
                                                    UseCase.ROUTING))
    except ModelPolicyError as e:
        log.warning("振り分けを実行しません: %s", e)
        return Choice()
    result = await runner.run_model(
        config, runner.ExecutionRequest(workspace(config), recipe, None, "", "", read_only=True), prompt,
    )
    if result.is_error:
        log.warning("振り分けに失敗しました: %s", "; ".join(result.errors)[:200])
        return Choice()
    choice = parse(result.text, allowed)
    # 相手も選ぶとき（研究全体のチャンネル）だけ、誰に渡したかを出す。
    # 相手が1人のときは、チャンネルで決まっているので仕事の名前だけでよい
    who = f"{choice.agent or '本体'} の " if fallback == SELF else ""
    log.info("振り分け: %s%s（%s）", who, choice.skill, choice.params or "指定なし")
    return choice
