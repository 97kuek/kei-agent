"""研究エージェント（A2A）に、claude の1回分を頼む。

`[a2a.agents]` に `research` を書いたときだけ使う。書かなければ、今までどおり同じプロセスで
`runner.run_claude` を動かす（docs/design.md の11章）。

頼み方も返事も JSON。claude は数分〜数十分かかるので、流しながら返してもらう（A2A の
SendStreamingMessage）。経過（使った道具と、返答の断片）が届くたびに、入力欄の下の1行に出す。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from kei_agent import a2a, agents, jobs, runner
from kei_agent.config import Config
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


def ask_payload(ws: Workspace, prompt: str, session_id: str | None, channel: str, thread_ts: str) -> str:
    return json.dumps({
        "channel_name": ws.channel_name,
        "prompt": prompt,
        "session_id": session_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "allowed_domains": list(ws.allowed_domains),
    }, ensure_ascii=False)


def to_result(data: dict) -> runner.RunResult:
    """封筒の `data` を RunResult に戻す。"""
    result = runner.RunResult(**{k: v for k, v in data.items() if k in FIELDS})
    # JSON では組が配列になるので、戻しておく（接続先の許可を聞くときに使う）
    result.requested_domains = [(d[0], d[1]) for d in result.requested_domains if len(d) >= 2]
    return result


async def run(agent: a2a.Agent, ws: Workspace, prompt: str, session_id: str | None,
              channel: str, thread_ts: str,
              on_activity: Callable[[str], Awaitable[None]] | None = None,
              on_text: Callable[[str], Awaitable[None]] | None = None) -> runner.RunResult:
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
                             text=ask_payload(ws, prompt, session_id, channel, thread_ts))
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
