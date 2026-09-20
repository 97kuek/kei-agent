"""頼まれた仕事をこなすところ。

A2A では、相手からのメッセージは `RequestContext` に入って届き、結果は `EventQueue` に流す。
ここでは、metadata の `skill`（なければ本文の1行目）で、どの仕事かを決める。
細かい指定（`days` など）も metadata で受け取る。

返事は全エージェント共通の封筒（`kei_agent_a2a/envelope.py`）。締切の一覧は `data.items` に入れ、
見せ方はオーケストレーターが決める（朝の一覧、24時間前の知らせ、スレッドへの返事で形が違う）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent.config import Config, load_config
from kei_agent.notion import NotionError
from kei_agent.timelog import TogglError
from kei_agent_a2a import claude, envelope
from kei_agent_course import moodle, notion_sync, periods, toggl_report, tools
from kei_agent_course.card import ASK, LIST_CLASSES, LIST_DUE, SYNC_ASSIGNMENTS, TIME_REPORT
from kei_agent_course.ics import Event

log = logging.getLogger(__name__)

SKILLS = (SYNC_ASSIGNMENTS, LIST_DUE, LIST_CLASSES, TIME_REPORT, ASK)
NO_ICS = ("Moodle のカレンダーの URL がありません。Moodle のカレンダー画面で「カレンダーをエクスポートする」から "
          f"URL を作って、秘密情報のファイルの {moodle.ICS_ENV} に入れてください")
# 一度に返す締切の数（声やスレッドで読める長さに収める）
MAX_DUE = 20
# 「締切の近い課題」で見る先の長さ（日）。取り込みは学期の終わりまで見るので、こちらだけ短くする
DUE_DAYS = 14


def asked_skill(text: str, metadata: dict | None = None) -> str:
    """どの仕事を頼まれたか。metadata の skill を優先し、なければ本文の1行目から探す。"""
    asked = (metadata or {}).get("skill")
    if not isinstance(asked, str):
        asked = (text or "").strip().splitlines()[0].strip() if text.strip() else ""
    return asked if asked in SKILLS else ""


def asked_days(metadata: dict | None, default: int) -> int:
    """metadata の days（何日先まで／何日ぶん）。数字でなければ既定のまま。"""
    try:
        days = int((metadata or {}).get("days", default))
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= 400 else default


def message_text(context: RequestContext) -> str:
    """届いたメッセージの本文（text の Part をつないだもの）。"""
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


def due_data(events: list[Event], days: int) -> dict:
    """締切の中身（見せ方はオーケストレーターが決める）。"""
    return {
        "days": days,
        "more": max(0, len(events) - MAX_DUE),
        "items": [{"id": e.uid, "at": e.starts_at.astimezone().isoformat(), "course": e.course_name,
                   "title": e.summary, "url": e.url} for e in events[:MAX_DUE]],
    }


class CourseExecutor(AgentExecutor):
    def __init__(self, config: Config | None = None):
        self.config = config or load_config()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        text = message_text(context)
        skill = asked_skill(text, metadata)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        if not skill:
            await self._fail(updater, f"どの仕事か分かりませんでした。{' / '.join(SKILLS)} のどれかを "
                                      "metadata の skill か、本文の1行目に書いてください")
            return
        log.info("頼まれた仕事: %s", skill)
        handlers = {SYNC_ASSIGNMENTS: self._sync_assignments, LIST_DUE: self._list_due,
                    LIST_CLASSES: self._list_classes, TIME_REPORT: self._time_report, ASK: self._ask}
        await handlers[skill](updater, metadata, text)

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([_text(envelope.failure(reason))]))

    async def _done(self, updater: TaskUpdater, text: str, data: dict | None = None) -> None:
        await claude.finish(updater, envelope.reply(text, data))

    async def _due_events(self, updater: TaskUpdater, days: int) -> list[Event] | None:
        """Moodle のカレンダーから締切を読む。読めなければ理由を返して None。"""
        url = moodle.ics_url()
        if not url:
            await self._fail(updater, NO_ICS)
            return None
        try:
            return await asyncio.to_thread(lambda: moodle.due(url, days=days))
        except moodle.MoodleError as e:
            await self._fail(updater, str(e))
            return None

    async def _sync_assignments(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """Moodle の締切を Notion の「課題」に反映する。"""
        events = await self._due_events(updater, asked_days(metadata, moodle.WINDOW_DAYS))
        if events is None:
            return
        try:
            result = await asyncio.to_thread(notion_sync.sync, events)
        except (notion_sync.SyncError, NotionError) as e:
            await self._fail(updater, str(e))
            return
        await self._done(updater, result.summary(), {
            "added": result.added, "updated": result.updated, "unchanged": result.unchanged,
            "other_courses": result.other_courses})

    async def _list_due(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """締切の近い課題を、近い順に JSON で返す。"""
        days = asked_days(metadata, DUE_DAYS)
        events = await self._due_events(updater, days)
        if events is None:
            return
        data = due_data(events, days)
        await self._done(updater, f"これから {days} 日で締切の課題は {len(data['items'])} 件", data)

    async def _list_classes(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """その曜日の授業を、時刻つきで返す（朝のまとめで時系列に並べるために使う）。"""
        day = date.today()
        weekday = str(metadata.get("weekday") or periods.weekday_of(day))
        try:
            found = await asyncio.to_thread(notion_sync.courses_on, weekday, day)
        except (notion_sync.SyncError, NotionError) as e:
            await self._fail(updater, str(e))
            return
        items = []
        for course in found:
            span = periods.at(day, course["period"])
            items.append({**course,
                          "start": span[0].isoformat(timespec="minutes") if span else "",
                          "end": span[1].isoformat(timespec="minutes") if span else ""})
        await self._done(updater, f"{weekday}曜の授業は {len(items)} コマ",
                         {"weekday": weekday, "items": items})

    async def _time_report(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """Toggl の記録を、科目ごと・課題ごとに集計して返す。"""
        note = ""
        try:
            courses = await asyncio.to_thread(notion_sync.course_names)
        except (notion_sync.SyncError, NotionError) as e:
            # 科目が読めないだけで集計をやめるより、全部数えて、そう言ったほうが役に立つ
            courses, note = None, f"\n（「授業」を読めなかったので、Toggl のプロジェクト全部を数えたよ: {e}）"
            log.warning("「授業」を読めませんでした: %s", e)
        try:
            text = await asyncio.to_thread(
                toggl_report.report, asked_days(metadata, toggl_report.DEFAULT_DAYS), courses)
        except TogglError as e:
            await self._fail(updater, str(e))
            return
        await self._done(updater, text + note)

    async def _ask(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        """定型に当てはまらない質問に、自分の claude が答える（連携で Box と Notion を読む）。"""
        question = (text or "").strip()
        if not question:
            await self._fail(updater, "質問が空です")
            return
        guide = self.config.repo_root / "prompts" / f"{tools.AGENT}.md"
        prompt = (f"{guide.read_text(encoding='utf-8') if guide.exists() else ''}\n\n---\n\n"
                  f"今日は {date.today().isoformat()}（{periods.weekday_of(date.today())}曜）。"
                  f"次の質問に答えてください。\n\n{question}")
        try:
            answer = await claude.ask_connector(self.config, prompt, tools.ALLOWED, tools.DENY,
                                                tools.TIMEOUT_MINUTES)
        except claude.ConnectorError as e:
            await self._fail(updater, str(e))
            return
        await claude.finish(updater, envelope.reply(answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def _text(value: str) -> Part:
    return Part(text=value)
