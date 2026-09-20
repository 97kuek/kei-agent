"""研究エージェント（A2A）に、claude の1回分を頼む。

`[a2a.agents]` に `research` を書いたときだけ使う。書かなければ、今までどおり同じプロセスで
`runner.run_claude` を動かす（docs/plan.md の15章のフェーズ②）。

頼み方も返事も JSON。claude は数分〜数十分かかるので、流しながら返してもらう（A2A の
SendStreamingMessage）。経過（使った道具と、返答の断片）が届くたびに、入力欄の下の1行に出す。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from kei_agent import a2a, agents, runner
from kei_agent.themes import Workspace

log = logging.getLogger(__name__)

AGENT = "research"
RUN_CLAUDE = "run-claude"
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
