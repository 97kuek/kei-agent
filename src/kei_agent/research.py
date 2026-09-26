"""研究エージェント（A2A）に、選択済み provider の1回分を頼む。

`[a2a.agents]` に `research` を書いたときだけ使う。書かなければ、今までどおり同じプロセスで
`runner.run_model` を動かす（docs/architecture.md）。

頼み方も返事も JSON。数分〜数十分かかることがあるため、A2A の SendStreamingMessage を使う。
入力欄の下に出すのは固定の利用者向け状態だけで、道具名や返答の断片は出さない。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable

from kei_agent import a2a, agents, jobs, runner
from kei_agent.config import Config
from kei_agent.model_policy import UseCase
from kei_agent.themes import Workspace
from kei_agent_research.skills import CANCEL_JOB, FORGET_JOB, LIST_JOBS, SUBMIT_JOB

log = logging.getLogger(__name__)

AGENT = "research"
_OVERRIDE = re.compile(r"^\s*\[\[([a-z][a-z0-9-]{0,39})\]\]\s*", re.IGNORECASE)
_LABELS = {
    "research-extract": UseCase.RESEARCH_EXTRACT,
    "research-screen": UseCase.RESEARCH_SCREEN,
    "research-compare": UseCase.RESEARCH_COMPARE,
    "research-execute": UseCase.RESEARCH_EXECUTE,
    "research-design": UseCase.RESEARCH_DESIGN,
    # 依頼者が明示した例外。通常の分類器は絶対に選ばない。
    "manual-astra": UseCase.MANUAL_ASTRA,
    "manual-fable": UseCase.MANUAL_FABLE,
}
_DEEP_WORDS = ("研究設計", "仮説", "実験計画", "手法選択", "比較設計", "厳密なレビュー")
_EXTRACT_WORDS = ("notion", "w&b", "wandb", "run", "metric", "artifact", "記録", "ログ", "一覧", "確認")
_COMPARE_WORDS = ("比較", "結果", "考察", "差分", "レビュー")


def use_case_for_prompt(prompt: str) -> tuple[UseCase, str]:
    """固定 skill の外から来た研究依頼を、安全な用途に分類する。"""
    match = _OVERRIDE.match(prompt)
    if match and (case := _LABELS.get(match.group(1).lower())) is not None:
        return case, prompt[match.end():].strip()
    text = prompt.strip()
    lowered = text.lower()
    if any(word in text for word in _DEEP_WORDS):
        return UseCase.RESEARCH_DESIGN, text
    if any(word in text for word in _COMPARE_WORDS):
        return UseCase.RESEARCH_COMPARE, text
    if any(word in lowered or word in text for word in _EXTRACT_WORDS):
        return UseCase.RESEARCH_EXTRACT, text
    return UseCase.RESEARCH_EXECUTE, text


def has_explicit_use_case(prompt: str) -> bool:
    match = _OVERRIDE.match(prompt)
    return bool(match and match.group(1).lower() in _LABELS)


def is_manual_use_case(use_case: UseCase) -> bool:
    return use_case in {UseCase.MANUAL_ASTRA, UseCase.MANUAL_FABLE}


def ask_payload(ws: Workspace, prompt: str, session_id: str | None, channel: str, thread_ts: str,
                use_case: UseCase = UseCase.RESEARCH_EXECUTE, *, provider: str = "",
                read_only: bool = False) -> dict:
    """研究の `ask` の依頼。ほかの担当と同じ形に、テーマの作業場と許可済みの接続先を足したもの。"""
    return {
        "channel_name": ws.channel_name,
        "prompt": prompt,
        "session_id": session_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "allowed_domains": list(ws.allowed_domains),
        "use_case": use_case.value,
        "provider": provider,
        "read_only": read_only,
    }


async def run(agent: a2a.Agent, ws: Workspace, prompt: str, session_id: str | None,
              channel: str, thread_ts: str,
              use_case: UseCase = UseCase.RESEARCH_EXECUTE,
              on_activity: Callable[[str], Awaitable[None]] | None = None,
              *, provider: str = "", read_only: bool = False) -> runner.RunResult:
    """研究エージェントに、テーマの作業場で provider を1回動かしてもらう。"""
    return await agents.run_ask(agent, ask_payload(ws, prompt, session_id, channel, thread_ts, use_case,
                                                   provider=provider, read_only=read_only), on_activity)


# 長い処理（ジョブ）


class RemotePueue:
    """研究エージェント越しの pueue。`jobs.Pueue` と同じ使い方ができる。"""

    def __init__(self, agent: a2a.Agent):
        self.agent = agent

    async def _ask(self, skill: str, body: dict | None = None) -> agents.Reply:
        reply = await agents.ask(self.agent, skill, text=json.dumps(body or {}, ensure_ascii=False))
        if not reply.ok:
            # jobs.Pueue と同じ形で失敗を返す（JobManager の扱いを変えずに済む）
            raise RuntimeError(reply.text or f"{skill} に失敗しました")
        return reply

    async def ensure_group(self) -> None:
        """待ち行列の用意は、相手が最初の投入のときに行う。"""
        return None

    async def add(self, cwd, command: str, label: str) -> int:
        reply = await self._ask(SUBMIT_JOB, {"cwd": str(cwd), "command": command, "label": label})
        try:
            return int(reply.data["task_id"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(f"ジョブの番号が返りませんでした: {reply.text[:200]}") from None

    async def kill(self, task_id: int) -> None:
        await self._ask(CANCEL_JOB, {"task_id": int(task_id)})

    async def remove(self, task_id: int) -> None:
        await self._ask(FORGET_JOB, {"task_id": int(task_id)})

    async def tasks(self) -> dict[int, dict]:
        reply = await self._ask(LIST_JOBS)
        found = reply.data.get("tasks") or {}
        return {int(k): v for k, v in found.items()}


def pueue(config: Config) -> jobs.Pueue | RemotePueue:
    """ジョブの待ち行列。研究エージェントがいれば、そちらの pueue を使う。"""
    url = config.a2a.url(AGENT)
    if not url:
        return jobs.Pueue(config)
    log.info("ジョブは研究エージェントの pueue を使います（%s）", url)
    return RemotePueue(a2a.Agent(url, config.a2a_token, timeout=config.a2a.timeout_seconds))
