"""頼まれた仕事をこなすところの土台（大学・研究・仕事のエージェントで共通）。

A2A では、相手からのメッセージは `RequestContext` に入って届き、結果は `EventQueue` に流す。
ここは「仕事を受け付けたと知らせ、本文と metadata を取り出して `handle` に渡す」まで。
どの仕事をどうこなすかは、エージェントごとの `handle` が決める。
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent_a2a import claude, envelope

log = logging.getLogger(__name__)

# metadata の days で受け付ける上限（日）
MAX_DAYS = 400


def message_text(context: RequestContext) -> str:
    """届いたメッセージの本文（text の Part をつないだもの）。"""
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


def asked_days(metadata: dict | None, default: int, maximum: int = MAX_DAYS) -> int:
    """metadata の days（何日先まで／何日ぶん）。数字でないか範囲の外なら既定のまま。"""
    try:
        days = int((metadata or {}).get("days", default))
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= maximum else default


class SkillExecutor(AgentExecutor):
    """仕事を受け付けて `handle` に渡す。返事は全エージェント共通の封筒（`envelope.py`）。"""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        text = message_text(context)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        await self.handle(updater, metadata, text)

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        raise NotImplementedError

    async def _fail(self, updater: TaskUpdater, reason: str, limit_reset_at: float | None = None) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([
            Part(text=envelope.reply(reason, ok=False, limit_reset_at=limit_reset_at))]))

    async def _done(self, updater: TaskUpdater, text: str, data: dict | None = None) -> None:
        await claude.finish(updater, envelope.reply(text, data))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
