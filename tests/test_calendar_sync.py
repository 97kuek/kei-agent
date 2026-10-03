"""Calendar sync never mistakes an incomplete source for deleted events."""

from datetime import datetime

import pytest

from kei_agent.scheduling.calendar_sync import (
    CalendarItem,
    CalendarSnapshot,
    IncompleteSnapshot,
    SyncReport,
    outlook_items,
    sync_calendar,
)


class FakeCalendarHub:
    def __init__(self):
        self.rows = [
            {"id": "manual", "出典": "手入力", "出典 ID": "", "名前": "会議", "同期状態": ""},
        ]
        self.writes = []
        self.windows = []

    def calendar_rows(self, source, window_start, window_end):
        self.windows.append((window_start, window_end))
        return [row.copy() for row in self.rows if row["出典"] == source
                and (not row.get("日付") or window_start.isoformat() <= row["日付"][:10] <= window_end.isoformat())]

    def calendar_upsert(self, source, item, checked_at, existing_id=None):
        self.writes.append((source, item.source_id, existing_id))
        if existing_id:
            row = next(row for row in self.rows if row["id"] == existing_id)
            row.update({"名前": item.title, "日付": item.start, "同期状態": "確認済み"})
        else:
            self.rows.append({"id": f"row-{len(self.rows)}", "出典": source,
                              "出典 ID": item.source_id, "名前": item.title,
                              "日付": item.start, "同期状態": "確認済み"})

    def calendar_mark_stale(self, row_id):
        self.writes.append(("stale", row_id))
        next(row for row in self.rows if row["id"] == row_id)["同期状態"] = "要確認"


CHECKED = datetime.fromisoformat("2026-09-24T10:00:00+09:00")


def outlook(*items, complete=True):
    return CalendarSnapshot("Outlook", complete, items)


def row(row_id, source_id, day, title="会議"):
    return {"id": row_id, "出典": "Outlook", "出典 ID": source_id, "名前": title, "日付": day, "同期状態": "確認済み"}


def item(source_id, title="会議", day="2026-09-25"):
    return CalendarItem(source_id, title, f"{day}T11:00:00+09:00",
                        f"{day}T12:00:00+09:00", "https://outlook.example/event", "会議室", "")


def test_same_name_different_outlook_ids_stay_distinct_and_manual_row_is_untouched():
    hub = FakeCalendarHub()
    sync_calendar(hub, outlook(item("event-1"), item("event-2")), CHECKED)
    sync_calendar(hub, outlook(item("event-1"), item("event-2")), CHECKED)

    assert [r["出典 ID"] for r in hub.rows if r["出典"] == "Outlook"] == ["event-1", "event-2"]
    assert hub.rows[0] == {"id": "manual", "出典": "手入力", "出典 ID": "", "名前": "会議", "同期状態": ""}


def test_empty_incomplete_read_does_not_flag_existing_rows():
    """AI が読んだ一覧（全部とは言い切れない）が0件なら、読み損ねを疑って何も触らない。"""
    hub = FakeCalendarHub()
    sync_calendar(hub, outlook(item("event-1")), CHECKED)
    before, writes = [row.copy() for row in hub.rows], len(hub.writes)

    assert sync_calendar(hub, outlook(complete=False), CHECKED, 7) == SyncReport(0, 0, 0)
    assert hub.rows == before and len(hub.writes) == writes


def test_incomplete_read_writes_what_it_found_and_flags_the_rest_in_its_window():
    hub = FakeCalendarHub()
    sync_calendar(hub, outlook(item("event-1"), item("far", day="2026-10-10")), CHECKED)

    report = sync_calendar(hub, outlook(item("event-2", day="2026-09-26"), complete=False), CHECKED, 7)

    states = {row["出典 ID"]: row["同期状態"] for row in hub.rows if row["出典"] == "Outlook"}
    assert report == SyncReport(1, 0, 1)
    # 7日の範囲の中で見えなくなった会議だけ「要確認」。範囲の外（10/10）は触らない
    assert states == {"event-1": "要確認", "far": "確認済み", "event-2": "確認済み"}


@pytest.mark.parametrize("days, created", [(None, ["event-1"]), (400, ["event-1", "far"])])
def test_items_outside_the_window_are_dropped_unless_the_window_is_long(caplog, days, created):
    """範囲の外の予定は捨てて同期は続ける。課題のように長い範囲（400日）なら遠い締切も写す。"""
    hub = FakeCalendarHub()
    far = CalendarItem("far", "Assignment J", "2027-02-01", "", "https://notion.so/a", "", "未着手")  # 日付だけの締切
    snapshot = CalendarSnapshot("課題", True, (item("event-1"), far))

    report = sync_calendar(hub, snapshot, CHECKED, *([days] if days else []))

    assert report.created == len(created)
    assert [r["出典 ID"] for r in hub.rows if r["出典"] == "課題"] == created
    assert ("範囲外の予定 1 件" in caplog.text) is (days is None)


def test_outlook_items_skip_unreadable_meetings_and_keep_join_links():
    events = [
        {"id": "event-1", "subject": "定例", "start": "2026-09-25T11:00", "end": "2026-09-25T12:00",
         "url": "https://teams.microsoft.com/l/meetup-join/x", "location": "https://zoom.us/j/1 パスコード: 123456"},
        {"id": "event-2", "subject": "時刻が読めない", "start": "来週"},
        {"id": "event-3", "subject": "", "start": "2026-09-25T13:00"},
        {"id": "event-1", "subject": "重なった ID", "start": "2026-09-25T14:00"},
        {"subject": "ID なし", "start": "2026-09-26T10:00", "url": "https://outlook.office.com/calendar/item/1"},
        "壊れた行",
    ]

    first, second = outlook_items(events), outlook_items(events)

    assert [i.title for i in first] == ["定例", "ID なし"]
    assert first[0].url == events[0]["url"] and first[0].location == events[0]["location"]
    assert sync_calendar(FakeCalendarHub(), outlook(first[0]), CHECKED).created == 1
    assert first[1].source_id.startswith("fallback:") and first == second   # ID が無くても毎回同じ


def test_complete_read_flags_missing_rows_in_its_window_only():
    """全部読めた一覧から消えた予定は、消さずに「要確認」。範囲の外（過去）と日付の無い行は触らない。

    既存の行は、範囲の少し外まで含めて引く。
    """
    hub = FakeCalendarHub()
    sync_calendar(hub, outlook(item("event-1")), CHECKED)
    hub.rows += [row("past", "past-event", "2026-09-20T11:00:00+09:00", "先週の会議"),
                 row("no-date", "event-2", "")]

    sync_calendar(hub, outlook(), CHECKED)

    assert {r["id"]: r["同期状態"] for r in hub.rows[1:]} == {"row-1": "要確認", "past": "確認済み", "no-date": "確認済み"}
    start, end = hub.windows[-1]
    assert start.isoformat() < "2026-09-24" and end.isoformat() > "2026-10-23"


def test_separate_meeting_credentials_survive_calendar_sync(monkeypatch):
    join_url = "https://teams.microsoft.com/l/meetup-join/" + "x" * 300
    meeting = {"id": "event-1", "subject": "定例", "start": "2026-09-25T11:00",
               "url": "https://outlook.example/event/1", "location": "会議室",
               "join_url": join_url, "passcode": "123456"}
    hub = FakeCalendarHub()
    written = []
    monkeypatch.setattr(hub, "calendar_upsert", lambda source, item, checked_at, existing_id: written.append(item))

    sync_calendar(hub, outlook(*outlook_items([meeting])), CHECKED)

    assert written[0].url == meeting["url"]
    assert join_url in written[0].location and "パスコード: 123456" in written[0].location


@pytest.mark.parametrize("existing, snapshot, problem", [
    (None, outlook(item("same"), item("same")), "重複"),
    (row("bad", "old-event", "2026-09-3x"), outlook(item("new-event")), "日付"),
    (None, CalendarSnapshot("手入力", True, ()), "出典"),     # 手で入れた行は、どの同期も出典にできない
])
def test_a_bad_snapshot_or_row_stops_before_any_write(existing, snapshot, problem):
    hub = FakeCalendarHub()
    if existing:
        hub.rows.append(existing)
    with pytest.raises(IncompleteSnapshot, match=problem):
        sync_calendar(hub, snapshot, CHECKED)
    assert hub.writes == []


@pytest.mark.parametrize("old_day", ["2026-09-20T11:00:00+09:00", ""])
def test_a_moved_or_date_cleared_event_reuses_its_row(old_day):
    """日付が範囲の外へ動いた予定も、手で日付を消した予定も、同じ出典 ID の行を書き直す（行を増やさない）。"""
    hub = FakeCalendarHub()
    hub.rows.append(row("old", "event-1", old_day, "旧予定"))

    sync_calendar(hub, outlook(item("event-1")), CHECKED)

    assert ("Outlook", "event-1", "old") in hub.writes
    assert len(hub.rows) == 2 and hub.rows[-1]["日付"] == "2026-09-25T11:00+09:00"


def test_a_module_source_is_written_with_its_own_name():
    """予定を出すモジュールは、自分の出典で書ける。"""
    hub = FakeCalendarHub()
    assert sync_calendar(hub, CalendarSnapshot("Google", False, (item("g1"),)), CHECKED) == SyncReport(1, 0, 0)
    assert hub.rows[0]["出典"] == "手入力" and hub.rows[1]["出典"] == "Google"
