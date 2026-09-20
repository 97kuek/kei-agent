"""頼まれた作業（claude を1回動かす）をこなすところ。

依頼は JSON で届く。

    {"channel_name": "amr-query", "prompt": "図を作って", "session_id": null,
     "channel": "C1", "thread_ts": "1.2", "allowed_domains": ["example.com"]}

返すのは全エージェント共通の封筒（`kei_agent_a2a/envelope.py`）で、`data` には `RunResult` が入る。
経過と柵の扱いは `kei_agent_a2a/claude.py`（大学エージェントと共通）。

会話の続け方（session の付け替え、履歴の戻し）と Slack への見せ方は持たない。
それはオーケストレーターの仕事（docs/plan.md の15章）。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent import themes
from kei_agent.config import Config, load_config
from kei_agent_a2a import claude, envelope
from kei_agent_research.card import RUN_CLAUDE

log = logging.getLogger(__name__)


def message_text(context: RequestContext) -> str:
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


class ResearchExecutor(AgentExecutor):
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        text = message_text(context)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        if metadata.get("skill", RUN_CLAUDE) != RUN_CLAUDE:
            await self._fail(updater, f"できるのは {RUN_CLAUDE} だけです")
            return
        try:
            ask = claude.ask_json(text)
            ws = themes.resolve(self.config, str(ask.get("channel_name") or ""))
        except (ValueError, KeyError) as e:
            await self._fail(updater, str(e))
            return
        if ws.cwd is None:
            await self._fail(updater, f"#{ws.channel_name} には作業用ディレクトリがありません")
            return
        ws = replace(ws, allowed_domains=tuple(ask.get("allowed_domains") or ()))
        themes.ensure_workspace(ws)
        await updater.complete(updater.new_agent_message(
            [Part(text=await claude.run(self.config, ws, ask, updater))]))

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([Part(text=envelope.failure(reason))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
