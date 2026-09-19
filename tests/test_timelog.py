import csv
import time
from datetime import date, datetime, timedelta

from ezra import timelog


def test_week_start():
    assert timelog.week_start(date(2026, 9, 19)) == date(2026, 9, 14)  # 土 → その週の月曜
    assert timelog.week_start(date(2026, 9, 14)) == date(2026, 9, 14)  # 月 → そのまま


def test_assistant_seconds_counts_only_finished_runs(store):
    now = time.time()
    run1 = store.start_run("C1", "1.1", "amr-query", "message")
    store.end_run(run1, False, None)
    store.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?", (now, now + 600, run1))
    run2 = store.start_run("C1", "2.1", "amr-query", "message")  # 終わっていない
    store.conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (now, run2))
    store.conn.commit()

    totals = timelog.assistant_seconds(store, now - 60, now + 60)

    assert totals == {(datetime.fromtimestamp(now).date().isoformat(), "amr-query"): 600}


def test_human_seconds_skips_running_entries():
    """動かしっぱなしの記録は duration が負で返る。まだ終わっていないので数えない。"""
    entries = [
        {"start": "2026-09-14T10:00:00+09:00", "duration": 3600, "project_id": 7},
        {"start": "2026-09-14T13:00:00+09:00", "duration": 1800, "project_id": 7},
        {"start": "2026-09-15T09:00:00+09:00", "duration": -1, "project_id": 7},   # 計測中
        {"start": "2026-09-15T11:00:00+09:00", "duration": 600},                    # プロジェクトなし
    ]
    totals = timelog.human_seconds(entries, {7: "amr-query"})

    assert totals[("2026-09-14", "amr-query")] == 5400
    assert totals[("2026-09-15", "-")] == 600
    assert ("2026-09-15", "amr-query") not in totals


class FakeToggl:
    def __init__(self, entries):
        self._entries = entries

    def entries(self, since, until):
        return self._entries

    def projects(self):
        return {7: "amr-query"}


def test_write_week_puts_both_columns_in_one_csv(config, store):
    monday = timelog.week_start(date.today())
    start = datetime.combine(monday, datetime.min.time()).timestamp() + 3600
    run = store.start_run("C1", "1.1", "amr-query", "message")
    store.end_run(run, False, None)
    store.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?", (start, start + 300, run))
    store.conn.commit()
    toggl = FakeToggl([{"start": f"{monday.isoformat()}T10:00:00+09:00", "duration": 7200, "project_id": 7}])

    path = timelog.write_week(config, store, toggl)

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    row = next(r for r in rows if r["テーマ"] == "amr-query")
    assert row["人の時間（分）"] == "120.0" and row["Ezra の稼働（分）"] == "5.0"
    assert path.name == f"{monday.isoformat()}.csv"


def test_write_week_works_without_toggl(config, store):
    path = timelog.write_week(config, store, None)
    assert path.exists()
    assert "人の時間が0" in "\n".join(timelog.week_summary(path))


def test_week_summary_totals_by_theme(config, store):
    monday = timelog.week_start(date.today())
    toggl = FakeToggl([
        {"start": f"{monday.isoformat()}T10:00:00+09:00", "duration": 3600, "project_id": 7},
        {"start": f"{(monday + timedelta(days=1)).isoformat()}T10:00:00+09:00", "duration": 1800, "project_id": 7},
    ])
    summary = "\n".join(timelog.week_summary(timelog.write_week(config, store, toggl)))
    assert "人 1.5 時間" in summary and "amr-query: 1.5 時間" in summary


def test_load_toggl_needs_a_token():
    assert timelog.load_toggl(env={}) is None
    assert isinstance(timelog.load_toggl(env={"TOGGL_API_TOKEN": "abc"}), timelog.Toggl)
