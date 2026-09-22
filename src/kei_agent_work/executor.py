"""頼まれた仕事（いまは Outlook の予定を読むだけ）をこなすところ。

予定・メール・資料・Teams は、会社の Claude アカウントに付いている Microsoft 365 の連携で読む
（`connector.py`）。

返すのは全エージェント共通の封筒（`kei_agent_a2a/envelope.py`）。見せ方はオーケストレーターが決める。
`list-events` が返すのは件名・時間・場所・リンクまで。朝のまとめで1行ずつ並べる形に合わせている
（docs/design.md の11章）。
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent.config import Config, load_config
from kei_agent.store import Store
from kei_agent_a2a import claude, envelope
from kei_agent_work import connector
from kei_agent_work.card import ASK, LIST_EVENTS

log = logging.getLogger(__name__)

SKILLS = (LIST_EVENTS, ASK)


def message_text(context: RequestContext) -> str:
    """届いたメッセージの本文。"""
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


def asked_days(metadata: dict | None, default: int = connector.DEFAULT_DAYS) -> int:
    try:
        days = int((metadata or {}).get("days", default))
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= 90 else default


class WorkExecutor(AgentExecutor):
    def __init__(self, config: Config | None = None, store: Store | None = None):
        self.config = config or load_config()
        self.store = store or Store(self.config.db_path)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        skill = metadata.get("skill", LIST_EVENTS)
        if skill not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} だけです")
            return
        if skill == ASK:
            await self._ask(updater, claude.ask_prompt(message_text(context)))
            return
        days = asked_days(metadata)
        try:
            events = await connector.events(self.config, days, store=self.store)
        except connector.WorkCalendarError as e:
            await self._fail(updater, str(e))
            return
        log.info("予定を %d 件返します（%d 日ぶん）", len(events), days)
        await claude.finish(updater, envelope.reply(
            f"これから {days} 日の予定は {len(events)} 件", {"days": days, "items": events}))

    async def _ask(self, updater: TaskUpdater, question: str) -> None:
        """自由な質問に、連携を読んで答える。"""
        if not question.strip():
            await self._fail(updater, "質問が空です")
            return
        try:
            answer = await connector.ask(self.config, question, store=self.store)
        except connector.WorkCalendarError as e:
            await self._fail(updater, str(e))
            return
        await claude.finish(updater, envelope.reply(answer))

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([Part(text=envelope.failure(reason))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
