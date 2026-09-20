"""頼まれた仕事（いまは Outlook の予定を読むだけ）をこなすところ。

予定の取り方は2つあり、既定は「会社の Claude アカウントに付いている Microsoft 365 の連携」
（`connector.py`）。Entra ID にアプリを作った場合は `WORK_CALENDAR_SOURCE=graph` で切り替える。

返すのは全エージェント共通の封筒（`kei_agent_a2a/envelope.py`）。見せ方はオーケストレーターが決める。
会社のデータなので、返すのは件名・時間・場所・リンクまでにし、本文は持ち出さない
（docs/plan.md の15章）。
"""

from __future__ import annotations

import asyncio
import logging
import os

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent.config import Config, load_config
from kei_agent_a2a import envelope
from kei_agent_work import connector, graph
from kei_agent_work.card import LIST_EVENTS

log = logging.getLogger(__name__)

SKILLS = (LIST_EVENTS,)
# 予定の取り方。既定は、会社の Claude アカウントに付いている Microsoft 365 の連携
SOURCE_ENV = "WORK_CALENDAR_SOURCE"
CONNECTOR, GRAPH = "connector", "graph"


def asked_days(metadata: dict | None, default: int = graph.DEFAULT_DAYS) -> int:
    try:
        days = int((metadata or {}).get("days", default))
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= 90 else default


class WorkExecutor(AgentExecutor):
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        if metadata.get("skill", LIST_EVENTS) not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} だけです")
            return
        days = asked_days(metadata)
        try:
            events = await self._events(days)
        except (connector.WorkCalendarError, graph.GraphError) as e:
            await self._fail(updater, str(e))
            return
        log.info("予定を %d 件返します（%d 日ぶん）", len(events), days)
        await updater.complete(updater.new_agent_message([Part(text=envelope.reply(
            f"これから {days} 日の予定は {len(events)} 件", {"days": days, "items": events}))]))

    async def _events(self, days: int) -> list[dict]:
        """予定の取り方を選ぶ。既定は連携、`WORK_CALENDAR_SOURCE=graph` なら Entra ID のアプリ。"""
        if os.environ.get(SOURCE_ENV, CONNECTOR) == GRAPH:
            return await asyncio.to_thread(lambda: graph.Graph.load().events(days=days))
        return await connector.events(self.config, days)

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([Part(text=envelope.failure(reason))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
