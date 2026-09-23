"""研究エージェント（A2A）に、claude の1回分を頼む。

`[a2a.agents]` に `research` を書いたときだけ使う。書かなければ、今までどおり同じプロセスで
`runner.run_model` を動かす（docs/design.md の11章）。

頼み方も返事も JSON。claude は数分〜数十分かかるので、流しながら返してもらう（A2A の
SendStreamingMessage）。経過（使った道具と、返答の断片）が届くたびに、入力欄の下の1行に出す。
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

log = logging.getLogger(__name__)

AGENT = "research"
RUN_CLAUDE = "run-claude"
SUBMIT_JOB = "submit-job"
LIST_JOBS = "list-jobs"
CANCEL_JOB = "cancel-job"
FORGET_JOB = "forget-job"
# RunResult のうち、相手から受け取る項目（知らない項目が増えても落ちないように、ここで絞る）
FIELDS = ("session_id", "text", "is_error", "cost_usd", "duration_ms", "errors", "activities",
          "timed_out", "requested_domains", "limit_reset_at")
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


def prepare(config: Config, ws: Workspace, prompt: str) -> tuple[Workspace, str]:
    # compatibility: caller now resolves use case separately; workspace に model を載せない。
    _, clean_prompt = use_case_for_prompt(prompt)
    return ws, clean_prompt


def ask_payload(ws: Workspace, prompt: str, session_id: str | None, channel: str, thread_ts: str,
                use_case: UseCase = UseCase.RESEARCH_EXECUTE, *, read_only: bool = False) -> str:
    return json.dumps({
        "channel_name": ws.channel_name,
        "prompt": prompt,
        "session_id": session_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "allowed_domains": list(ws.allowed_domains),
        "use_case": use_case.value,
        "read_only": read_only,
    }, ensure_ascii=False)


def to_result(data: dict) -> runner.RunResult:
    """封筒の `data` を RunResult に戻す。"""
    result = runner.RunResult(**{k: v for k, v in data.items() if k in FIELDS})
    # JSON では組が配列になるので、戻しておく（接続先の許可を聞くときに使う）
    result.requested_domains = [(d[0], d[1]) for d in result.requested_domains if len(d) >= 2]
    return result


async def run(agent: a2a.Agent, ws: Workspace, prompt: str, session_id: str | None,
              channel: str, thread_ts: str,
              use_case: UseCase = UseCase.RESEARCH_EXECUTE,
              on_activity: Callable[[str], Awaitable[None]] | None = None,
              on_text: Callable[[str], Awaitable[None]] | None = None,
              *, read_only: bool = False) -> runner.RunResult:
    """研究エージェントに claude を1回動かしてもらう。"""

    async def on_progress(payload: str) -> None:
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if on_activity and event.get("activity"):
            await on_activity(event["activity"])
        if on_text and event.get("text"):
            await on_text(event["text"])

    reply = await agents.ask(agent, RUN_CLAUDE, on_progress=on_progress,
                             text=ask_payload(ws, prompt, session_id, channel, thread_ts, use_case, read_only=read_only))
    if not reply.data:
        # 封筒が開けなかった（つながらない、途中で切れた、形が違う）
        return runner.RunResult(is_error=True, errors=[reply.text or "研究エージェントが返事をしませんでした"])
    return to_result(reply.data)


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
