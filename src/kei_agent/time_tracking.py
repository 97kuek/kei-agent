"""Slack の時間カードが使う、1人1本のローカル計測。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Literal

from kei_agent.store import Store

Domain = Literal["research", "course", "work"]


@dataclass(frozen=True)
class TimerContext:
    user_id: str
    domain: Domain
    channel_id: str
    channel_name: str
    course_page_id: str = ""
    course_name: str = ""


@dataclass(frozen=True)
class TimeEntry:
    id: str
    user_id: str
    domain: Domain
    channel_id: str
    channel_name: str
    course_page_id: str
    course_name: str
    description: str
    memo: str
    started_at: float
    ended_at: float | None
    toggl_state: str
    notion_state: str


@dataclass(frozen=True)
class CourseBinding:
    channel_id: str
    course_page_id: str
    course_name: str


def project_label(domain: Domain, label: str) -> str:
    prefix = {"research": "研究", "course": "大学", "work": "仕事"}[domain]
    return f"{prefix} / {label.strip()}"


def _entry(row) -> TimeEntry:
    return TimeEntry(
        id=row["id"], user_id=row["user_id"], domain=row["domain"], channel_id=row["channel"],
        channel_name=row["channel_name"], course_page_id=row["course_page_id"], course_name=row["course_name"],
        description=row["description"], memo=row["memo"], started_at=row["started_at"], ended_at=row["ended_at"],
        toggl_state=row["toggl_state"], notion_state=row["notion_state"],
    )


class TimeTracker:
    def __init__(self, store: Store):
        self.store = store

    def start(self, context: TimerContext, started_at: float | None = None) -> tuple[TimeEntry, TimeEntry | None]:
        if not context.user_id or not context.channel_id or not context.channel_name:
            raise ValueError("利用者とチャンネルを指定してください")
        label = context.course_name if context.domain == "course" and context.course_name else context.channel_name
        entry, previous = self.store.start_time_entry(
            uuid.uuid4().hex, context.user_id, context.domain, context.channel_id, context.channel_name,
            context.course_page_id, context.course_name, project_label(context.domain, label),
            time.time() if started_at is None else started_at,
            "pending",
        )
        return _entry(entry), _entry(previous) if previous is not None else None

    def stop(self, user_id: str, ended_at: float | None = None) -> TimeEntry | None:
        if not user_id:
            raise ValueError("利用者を指定してください")
        row = self.store.finish_time_entry(user_id, time.time() if ended_at is None else ended_at)
        return _entry(row) if row is not None else None

    def active(self, user_id: str) -> TimeEntry | None:
        row = self.store.active_time_entry(user_id)
        return _entry(row) if row is not None else None

    def entry(self, entry_id: str) -> TimeEntry | None:
        row = self.store.time_entry(entry_id)
        return _entry(row) if row is not None else None

    def add_memo(self, entry_id: str, memo: str) -> TimeEntry:
        if not entry_id:
            raise ValueError("時間記録を指定してください")
        return _entry(self.store.set_time_memo(entry_id, memo.strip()[:1000]))

    def bind_course_channel(self, channel_id: str, course_page_id: str, course_name: str) -> None:
        if not channel_id or not course_page_id or not course_name.strip():
            raise ValueError("チャンネルと科目を指定してください")
        self.store.bind_course_channel(channel_id, course_page_id, course_name.strip())

    def course_binding(self, channel_id: str) -> CourseBinding | None:
        row = self.store.course_channel_binding(channel_id)
        return CourseBinding(row["channel"], row["course_page_id"], row["course_name"]) if row is not None else None
