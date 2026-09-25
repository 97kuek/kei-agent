"""Calendar sync never mistakes an incomplete source for deleted events."""

from datetime import datetime

import pytest

from kei_agent.calendar_sync import CalendarItem, CalendarSnapshot, IncompleteSnapshot, sync_calendar


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


def item(source_id, title="会議", day="2026-09-25"):
    return CalendarItem(source_id, title, f"{day}T11:00:00+09:00",
                        f"{day}T12:00:00+09:00", "https://outlook.example/event", "会議室", "")


def test_same_name_different_outlook_ids_stay_distinct_and_manual_row_is_untouched():
    hub = FakeCalendarHub()
    snapshot = CalendarSnapshot("Outlook", True, (item("event-1"), item("event-2")))
    checked = datetime.fromisoformat("2026-09-24T10:00:00+09:00")

    sync_calendar(hub, snapshot, checked)
    sync_calendar(hub, snapshot, checked)

    assert len(hub.rows) == 3
    assert [r["出典 ID"] for r in hub.rows if r["出典"] == "Outlook"] == ["event-1", "event-2"]
    assert hub.rows[0] == {"id": "manual", "出典": "手入力", "出典 ID": "", "名前": "会議", "同期状態": ""}


def test_incomplete_snapshot_does_not_touch_existing_rows():
    hub = FakeCalendarHub()
    checked = datetime.fromisoformat("2026-09-24T10:00:00+09:00")
    sync_calendar(hub, CalendarSnapshot("Outlook", True, (item("event-1"),)), checked)
    before = [row.copy() for row in hub.rows]
    writes = len(hub.writes)

    with pytest.raises(IncompleteSnapshot):
        sync_calendar(hub, CalendarSnapshot("Outlook", False, ()), checked)

    assert hub.rows == before
    assert len(hub.writes) == writes


def test_missing_item_from_complete_snapshot_is_marked_not_deleted():
    hub = FakeCalendarHub()
    checked = datetime.fromisoformat("2026-09-24T10:00:00+09:00")
    sync_calendar(hub, CalendarSnapshot("Outlook", True, (item("event-1"),)), checked)

    sync_calendar(hub, CalendarSnapshot("Outlook", True, ()), checked)

    assert len(hub.rows) == 2
    assert hub.rows[1]["同期状態"] == "要確認"


def test_duplicate_source_id_is_rejected_before_writing():
    hub = FakeCalendarHub()
    with pytest.raises(IncompleteSnapshot, match="重複"):
        sync_calendar(hub, CalendarSnapshot("Outlook", True, (item("same"), item("same"))),
                      datetime.fromisoformat("2026-09-24T10:00:00+09:00"))
    assert hub.writes == []


def test_past_event_outside_snapshot_window_is_not_marked_stale():
    hub = FakeCalendarHub()
    hub.rows.append({"id": "past", "出典": "Outlook", "出典 ID": "past-event",
                     "名前": "先週の会議", "日付": "2026-09-20T11:00:00+09:00", "同期状態": "確認済み"})

    sync_calendar(hub, CalendarSnapshot("Outlook", True, ()),
                  datetime.fromisoformat("2026-09-24T10:00:00+09:00"))

    assert hub.rows[-1]["同期状態"] == "確認済み"


def test_moved_event_reuses_existing_row_even_when_old_date_is_outside_window():
    hub = FakeCalendarHub()
    hub.rows.append({"id": "old", "出典": "Outlook", "出典 ID": "event-1",
                     "名前": "旧予定", "日付": "2026-09-20T11:00:00+09:00", "同期状態": "確認済み"})
    checked = datetime.fromisoformat("2026-09-24T10:00:00+09:00")

    sync_calendar(hub, CalendarSnapshot("Outlook", True, (item("event-1"),)), checked)

    assert len(hub.rows) == 2
    assert hub.rows[-1]["id"] == "old"
    assert hub.rows[-1]["日付"] == "2026-09-25T11:00+09:00"


def test_bad_existing_date_stops_before_any_write():
    hub = FakeCalendarHub()
    hub.rows.append({"id": "bad", "出典": "Outlook", "出典 ID": "old-event",
                     "名前": "古い予定", "日付": "2026-09-3x", "同期状態": "確認済み"})
    with pytest.raises(IncompleteSnapshot, match="日付"):
        sync_calendar(hub, CalendarSnapshot("Outlook", True, (item("new-event"),)),
                      datetime.fromisoformat("2026-09-24T10:00:00+09:00"))
    assert hub.writes == []


def test_item_outside_window_is_dropped_without_stopping_sync(caplog):
    hub = FakeCalendarHub()
    snapshot = CalendarSnapshot("Outlook", True, (item("event-1"), item("far", day="2026-12-01")))

    report = sync_calendar(hub, snapshot, datetime.fromisoformat("2026-09-24T10:00:00+09:00"))

    assert report.created == 1
    assert [r["出典 ID"] for r in hub.rows if r["出典"] == "Outlook"] == ["event-1"]
    assert "範囲外の予定 1 件" in caplog.text


def test_manually_cleared_date_is_rewritten_instead_of_stalling():
    hub = FakeCalendarHub()
    hub.rows.append({"id": "cleared", "出典": "Outlook", "出典 ID": "event-1",
                     "名前": "会議", "日付": "", "同期状態": "確認済み"})
    hub.rows.append({"id": "gone", "出典": "Outlook", "出典 ID": "event-2",
                     "名前": "消えた会議", "日付": "", "同期状態": "確認済み"})

    sync_calendar(hub, CalendarSnapshot("Outlook", True, (item("event-1"),)),
                  datetime.fromisoformat("2026-09-24T10:00:00+09:00"))

    assert ("Outlook", "event-1", "cleared") in hub.writes
    assert hub.rows[1]["日付"] == "2026-09-25T11:00+09:00"
    # 日付がない行は範囲内か分からないので、要確認にはしない
    assert hub.rows[2]["同期状態"] == "確認済み"


def test_existing_rows_are_looked_up_around_the_window():
    hub = FakeCalendarHub()
    sync_calendar(hub, CalendarSnapshot("Outlook", True, ()), datetime.fromisoformat("2026-09-24T10:00:00+09:00"))
    start, end = hub.windows[0]
    assert start.isoformat() < "2026-09-24" and end.isoformat() > "2026-10-23"
