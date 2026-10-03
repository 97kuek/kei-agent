"""完全な domain snapshot だけを共有 Notion カレンダーへ反映する。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from kei_agent.storage.notion_hub import HubStore

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
# 既存行を探すとき、取得範囲の前後に広げる日数
MATCH_MARGIN_DAYS = 60


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


# 手で入れた予定の出典。どの同期も、この行には触らない
MANUAL = "手入力"


@dataclass(frozen=True)
class CalendarSnapshot:
    # 出典（Outlook・課題のほか、予定を出すモジュールの出典）。「手入力」は使えない
    source: str
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
    if not snapshot.source or snapshot.source == MANUAL:
        raise IncompleteSnapshot("出典が分かりません")
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
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise IncompleteSnapshot("元 URL が予定ページではありません")
        items.append(CalendarItem(item.source_id, item.title[:200], start, end,
                                  item.url, item.location, item.status[:100]))
    return tuple(items)


def sync_calendar(hub: HubStore, snapshot: CalendarSnapshot, checked_at: datetime, days: int = 30) -> SyncReport:
    """source+ID だけで照合する。手入力・他出典は触らず、消えた行も消去しない。

    取得範囲（checked_at の日から days 日）の中で見えなくなった行には「要確認」を付ける
    （次に見えれば「確認済み」に戻る）。全部を読めたと言い切れない取得（complete=False。AI が読んだ
    Outlook）が0件のときは、読み損ねを疑って印を付けない。
    """
    items = _validated(snapshot)
    window_start = checked_at.astimezone(JST).date()
    window_end = window_start + timedelta(days=days - 1)
    inside = tuple(item for item in items if window_start <= date.fromisoformat(item.start[:10]) <= window_end)
    if len(inside) != len(items):
        # 範囲外の1件で全体を止めず、その行だけ書かない
        log.warning("%s: %d日間の取得範囲外の予定 %d 件を反映しません", snapshot.source, days, len(items) - len(inside))
        items = inside
    # 日付を動かした予定も同じ行に戻せるよう、範囲の前後も照合に使う
    rows = hub.calendar_rows(snapshot.source, window_start - timedelta(days=MATCH_MARGIN_DAYS),
                             window_end + timedelta(days=MATCH_MARGIN_DAYS))
    by_id = {}
    row_dates: dict[str, date | None] = {}
    for row in rows:
        source_id = row.get("出典 ID") or ""
        if not source_id or source_id in by_id:
            raise IncompleteSnapshot("既存カレンダー行の出典 ID が欠落または重複しています")
        raw = str(row.get("日付") or "")
        try:
            # 手で日付を消した行は、次の同期で書き直す（止めない）
            row_dates[source_id] = date.fromisoformat(raw[:10]) if raw else None
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
    if not snapshot.complete and not items:
        return SyncReport(created, updated, stale)
    current_ids = {item.source_id for item in items}
    for source_id, row in by_id.items():
        day = row_dates[source_id]
        if (source_id not in current_ids and day is not None
                and window_start <= day <= window_end
                and row.get("同期状態") != "要確認"):
            hub.calendar_mark_stale(row["id"])
            stale += 1
    return SyncReport(created, updated, stale)


def outlook_items(events: list[dict]) -> tuple[CalendarItem, ...]:
    """仕事の担当が読んだ会議を、カレンダーの行にする。

    AI が読んだ一覧なので、件名や時刻が読めない会議は飛ばす。参加リンクとパスコードは保持する。
    ID が無い会議には、件名・開始・リンクから毎回同じ ID を作る。
    """
    items: list[CalendarItem] = []
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        title = str(event.get("subject") or "").strip()
        start, end = str(event.get("start") or "").strip(), str(event.get("end") or "").strip()
        try:
            _datetime(start, "Outlook")
            if end:
                _datetime(end, "Outlook")
        except IncompleteSnapshot:
            continue
        if not title:
            continue
        url = str(event.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            url = ""
        location = str(event.get("location") or "")
        join_url = str(event.get("join_url") or "")
        join = urlparse(join_url)
        if join.scheme in {"http", "https"} and join.netloc:
            if join_url not in location:
                location = "\n".join(filter(None, (location, join_url)))
            url = url or join_url
        passcode = str(event.get("passcode") or "")
        if passcode and passcode not in location:
            location = "\n".join(filter(None, (location, f"パスコード: {passcode}")))
        source_id = str(event.get("id") or "").strip() or "fallback:" + hashlib.sha256(
            "\0".join((title, start, url)).encode("utf-8")).hexdigest()
        if source_id in seen:
            continue
        seen.add(source_id)
        items.append(CalendarItem(source_id, title, start, end, url, location, ""))
    return tuple(items)
