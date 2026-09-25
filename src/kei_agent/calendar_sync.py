"""完全な domain snapshot だけを共有 Notion カレンダーへ反映する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from kei_agent.notion_hub import HubStore

JST = ZoneInfo("Asia/Tokyo")


class IncompleteSnapshot(ValueError):
    pass


@dataclass(frozen=True)
class CalendarItem:
    source_id: str
    title: str
    start: str
    end: str
    url: str
    location: str
    status: str


@dataclass(frozen=True)
class CalendarSnapshot:
    source: Literal["Outlook", "課題"]
    complete: bool
    items: tuple[CalendarItem, ...]
    expected_count: int | None = None


@dataclass(frozen=True)
class SyncReport:
    created: int
    updated: int
    stale: int


def _datetime(value: str, source: str) -> str:
    if source == "課題" and len(value) == 10:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise IncompleteSnapshot("課題の締切が不正です") from None
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise IncompleteSnapshot("予定の時刻が不正です") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST).isoformat(timespec="minutes")


def _validated(snapshot: CalendarSnapshot) -> tuple[CalendarItem, ...]:
    if snapshot.source not in {"Outlook", "課題"} or not snapshot.complete:
        raise IncompleteSnapshot("出典の取得が完了していません")
    if snapshot.expected_count is not None and snapshot.expected_count != len(snapshot.items):
        raise IncompleteSnapshot("出典の件数が応答と一致しません")
    found = set()
    items = []
    for item in snapshot.items:
        if not item.source_id or item.source_id in found:
            raise IncompleteSnapshot("出典 ID が欠落または重複しています")
        found.add(item.source_id)
        if not item.title.strip() or not item.start:
            raise IncompleteSnapshot("予定の件名または開始時刻がありません")
        start = _datetime(item.start, snapshot.source)
        end = _datetime(item.end, snapshot.source) if item.end else ""
        if item.url:
            parsed = urlparse(item.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or "teams.microsoft.com" in parsed.netloc:
                raise IncompleteSnapshot("元 URL が予定ページではありません")
        if "http://" in item.location or "https://" in item.location:
            raise IncompleteSnapshot("場所に参加リンクが混ざっています")
        items.append(CalendarItem(item.source_id, item.title[:200], start, end,
                                  item.url, item.location[:200], item.status[:100]))
    return tuple(items)


def sync_calendar(hub: HubStore, snapshot: CalendarSnapshot, checked_at: datetime) -> SyncReport:
    """source+ID だけで照合。手入力・他出典は触らず、消えた行も消去しない。"""
    items = _validated(snapshot)
    window_start = checked_at.astimezone(JST).date()
    window_end = window_start + timedelta(days=29)
    if any(not window_start <= date.fromisoformat(item.start[:10]) <= window_end for item in items):
        raise IncompleteSnapshot("30日間の取得範囲外に予定が含まれています")
    rows = hub.calendar_rows(snapshot.source, window_start, window_end)
    by_id = {}
    row_dates = {}
    for row in rows:
        source_id = row.get("出典 ID") or ""
        if not source_id or source_id in by_id:
            raise IncompleteSnapshot("既存カレンダー行の出典 ID が欠落または重複しています")
        try:
            row_dates[source_id] = date.fromisoformat(str(row.get("日付") or "")[:10])
        except ValueError:
            raise IncompleteSnapshot("既存カレンダー行の日付が不正です") from None
        by_id[source_id] = row
    created = updated = stale = 0
    for item in items:
        existing = by_id.get(item.source_id)
        hub.calendar_upsert(snapshot.source, item, checked_at, existing["id"] if existing else None)
        if existing:
            updated += 1
        else:
            created += 1
    current_ids = {item.source_id for item in items}
    for source_id, row in by_id.items():
        if (source_id not in current_ids
                and window_start <= row_dates[source_id] <= window_end
                and row.get("同期状態") != "要確認"):
            hub.calendar_mark_stale(row["id"])
            stale += 1
    return SyncReport(created, updated, stale)
