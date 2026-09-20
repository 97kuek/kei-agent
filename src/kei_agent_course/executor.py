"""頼まれた仕事をこなすところ。

A2A では、相手からのメッセージは `RequestContext` に入って届き、結果は `EventQueue` に流す。
ここでは、メッセージの1行目（または metadata の `skill`）で、どの仕事かを決める。
中身はまだ空で、いまは「受け取ったこと」と「まだできないこと」を正直に返す。
"""

from __future__ import annotations

import asyncio
import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent_course import moodle
from kei_agent_course.card import LIST_DUE, SYNC_ASSIGNMENTS, TIME_REPORT

log = logging.getLogger(__name__)

# まだ中身がない仕事の返事（実装するまでの間、何が足りないかを相手に伝える）
NOT_READY = {
    SYNC_ASSIGNMENTS: "Notion の授業・課題データベースをまだ作っていないので、取り込み先がありません",
    TIME_REPORT: "Toggl の集計はまだ作っていません",
}
NO_ICS = ("Moodle のカレンダーの URL がありません。Moodle のカレンダー画面で「カレンダーをエクスポートする」から "
          f"URL を作って、秘密情報のファイルの {moodle.ICS_ENV} に入れてください")
# 一度に返す締切の数（声やスレッドで読める長さに収める）
MAX_DUE = 20


def asked_skill(text: str, metadata: dict | None = None) -> str:
    """どの仕事を頼まれたか。metadata の skill を優先し、なければ本文から探す。"""
    if metadata and isinstance(metadata.get("skill"), str):
        return metadata["skill"]
    head = (text or "").strip().splitlines()[0].strip() if text.strip() else ""
    return head if head in (SYNC_ASSIGNMENTS, LIST_DUE, TIME_REPORT) else ""


def message_text(context: RequestContext) -> str:
    """届いたメッセージの本文（text の Part をつないだもの）。"""
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


class CourseExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = message_text(context)
        skill = asked_skill(text, dict(getattr(context, "metadata", None) or {}))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        if not skill:
            await updater.failed(updater.new_agent_message(
                [_text(f"どの仕事か分かりませんでした。{SYNC_ASSIGNMENTS} / {LIST_DUE} / {TIME_REPORT} のどれかを "
                       "metadata の skill か、本文の1行目に書いてください")]))
            return
        log.info("頼まれた仕事: %s", skill)
        if skill == LIST_DUE:
            await self._list_due(updater)
            return
        await updater.complete(updater.new_agent_message([_text(NOT_READY[skill])]))

    async def _list_due(self, updater: TaskUpdater) -> None:
        """締切の近い課題を、近い順に短い行にして返す。"""
        url = moodle.ics_url()
        if not url:
            await updater.failed(updater.new_agent_message([_text(NO_ICS)]))
            return
        try:
            events = await asyncio.to_thread(moodle.due, url)
        except moodle.MoodleError as e:
            await updater.failed(updater.new_agent_message([_text(str(e))]))
            return
        if not events:
            await updater.complete(updater.new_agent_message([_text("締切の近い課題はありません")]))
            return
        lines = [f"{e.starts_at:%m/%d %H:%M} {e.course + ' ' if e.course else ''}{e.summary}"
                 for e in events[:MAX_DUE]]
        if len(events) > MAX_DUE:
            lines.append(f"（ほかに {len(events) - MAX_DUE} 件）")
        await updater.complete(updater.new_agent_message([_text("\n".join(lines))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def _text(value: str) -> Part:
    return Part(text=value)
