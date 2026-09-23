import csv
import time
from datetime import UTC, date, datetime, timedelta

from kei_agent import timelog


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


def _entry(start, duration, project="研究/amr-query", **extra):
    """Toggl 2.0 の time-entries が返す1件（使う項目だけ）。"""
    e = {"start": start, "duration": duration, "type": "activity",
         "project": {"id": 7, "name": project} if project else None}
    return e | extra


def test_human_seconds_skips_running_entries():
    """動かしっぱなしの記録は duration が負か空で返る。まだ終わっていないので数えない。"""
    entries = [
        _entry("2026-09-14T01:00:00Z", 3600),
        _entry("2026-09-14T04:00:00Z", 1800),
        _entry("2026-09-15T00:00:00Z", -1),       # 計測中
        _entry("2026-09-15T00:30:00Z", None),     # 計測中
        _entry("2026-09-15T02:00:00Z", 600, project=None),
    ]
    totals = timelog.human_seconds(entries)

    assert totals[("2026-09-14", "研究/amr-query")] == 5400
    assert totals[("2026-09-15", "-")] == 600
    assert ("2026-09-15", "研究/amr-query") not in totals


def test_human_seconds_skips_breaks_and_deleted():
    entries = [
        _entry("2026-09-14T01:00:00Z", 3600),
        _entry("2026-09-14T03:00:00Z", 900, type="break"),
        _entry("2026-09-14T04:00:00Z", 900, deleted_at="2026-09-14T05:00:00Z"),
    ]
    assert timelog.human_seconds(entries) == {("2026-09-14", "研究/amr-query"): 3600}


def test_human_seconds_uses_local_date():
    """UTC では前日でも、手元の時刻での日付に数える。"""
    start = datetime(2026, 9, 14, 0, 30).astimezone()   # 手元の 9/14 0:30
    entries = [_entry(start.astimezone(UTC).isoformat().replace("+00:00", "Z"), 60)]
    assert timelog.human_seconds(entries) == {("2026-09-14", "研究/amr-query"): 60}


def test_toggl_entries_reads_every_page():
    toggl = timelog.Toggl("key", organization_id=1, workspace_id=2)
    calls = []

    def fake_request(path, query):
        calls.append((path, query))
        n = timelog.PER_PAGE if query["page"] == 1 else 3
        return {"data": [_entry("2026-09-14T01:00:00Z", 60)] * n, "page": query["page"]}

    toggl._request = fake_request
    entries = toggl.entries(date(2026, 9, 14), date(2026, 9, 20))

    assert len(entries) == timelog.PER_PAGE + 3
    assert [q["page"] for _, q in calls] == [1, 2]
    path, query = calls[0]
    assert path == "/organizations/1/workspaces/2/time-entries"
    # タスクに付いていない記録も入れないと、プロジェクトだけで測った時間が1件も返らない
    assert query["include_taskless"] == "true"
    assert query["date_from"].endswith("Z") and query["date_to"].endswith("Z")


def test_toggl_records_one_completed_taskless_entry():
    toggl = timelog.Toggl("key", organization_id=7, workspace_id=8)
    posted = []

    def get(path, query):
        assert path == "/organizations/7/workspaces/8/projects"
        return {"data": [{"id": 41, "name": "大学 / マルチメディア工学A"}]}

    toggl._request = get
    toggl._post = lambda path, body: posted.append((path, body)) or {}
    started = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)

    toggl.record_completed("大学 / マルチメディア工学A", "大学 / マルチメディア工学A", started, 1500)

    assert posted == [(
        "/organizations/7/workspaces/8/time-entries/bulk",
        {"items": [{"project_id": 41, "description": "大学 / マルチメディア工学A",
                    "start": "2026-09-23T10:00:00+00:00", "duration": 1500, "type": "activity"}]},
    )]


class FakeToggl:
    def __init__(self, entries):
        self._entries = entries

    def entries(self, since, until):
        return self._entries


def test_write_week_puts_both_columns_in_one_csv(config, store):
    (config.research_root / "amr-query").mkdir(parents=True)
    monday = timelog.week_start(date.today())
    start = datetime.combine(monday, datetime.min.time()).timestamp() + 3600
    run = store.start_run("C1", "1.1", "amr-query", "message")
    store.end_run(run, False, None)
    store.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?", (start, start + 300, run))
    store.conn.commit()
    toggl = FakeToggl([_entry(f"{monday.isoformat()}T10:00:00+09:00", 7200)])

    path = timelog.write_week(config, store, toggl)

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    row = next(r for r in rows if r["プロジェクト"] == "amr-query")
    assert row["領域"] == "研究"
    assert row["人の時間（分）"] == "120.0" and row["Kei Agent の稼働（分）"] == "5.0"
    assert path.name == f"{monday.isoformat()}.csv"


def test_write_week_works_without_toggl(config, store):
    path = timelog.write_week(config, store, None)
    assert path.exists()
    assert "人の時間が0" in "\n".join(timelog.week_summary(path))


def test_week_summary_totals_by_domain(config, store):
    """まとめは、テーマや科目ごとではなく、研究・大学・仕事の3つでまとめる。"""
    (config.research_root / "amr-query").mkdir(parents=True)
    monday = timelog.week_start(date.today())
    toggl = FakeToggl([
        _entry(f"{monday.isoformat()}T10:00:00+09:00", 3600),
        _entry(f"{(monday + timedelta(days=1)).isoformat()}T10:00:00+09:00", 1800),
        _entry(f"{monday.isoformat()}T14:00:00+09:00", 3600, project="大学/データベース"),
    ])
    summary = "\n".join(timelog.week_summary(timelog.write_week(config, store, toggl)))
    assert "人 2.5 時間" in summary and "研究: 1.5 時間" in summary and "大学: 1.0 時間" in summary


def test_write_week_counts_only_the_marked_projects(config, store):
    """Toggl にはアルバイトや個人開発の時間も入っている。先頭に印のあるものだけを数える。"""
    (config.research_root / "amr-query").mkdir(parents=True)
    monday = timelog.week_start(date.today())
    toggl = FakeToggl([
        _entry(f"{monday.isoformat()}T10:00:00+09:00", 3600),
        _entry(f"{monday.isoformat()}T11:00:00+09:00", 1800, project="仕事/ゆうちょ"),
        _entry(f"{monday.isoformat()}T12:00:00+09:00", 7200, project="アルバイト"),
        _entry(f"{monday.isoformat()}T15:00:00+09:00", 600, project=None),
    ])

    rows = list(csv.DictReader(timelog.write_week(config, store, toggl).open(encoding="utf-8")))
    got = {(r["領域"], r["プロジェクト"]): r["人の時間（分）"] for r in rows}

    assert got == {("研究", "amr-query"): "60.0", ("仕事", "ゆうちょ"): "30.0"}


def test_split_project_needs_a_known_mark():
    assert timelog.split_project("研究/amr-query") == ("研究", "amr-query")
    assert timelog.split_project("大学/マルチメディア工学A") == ("大学", "マルチメディア工学A")
    assert timelog.split_project("amr-query") is None       # 印がない
    assert timelog.split_project("趣味/写真") is None        # 知らない印
    assert timelog.split_project("研究/") is None            # 名前がない


def test_load_toggl_needs_a_token_and_ids():
    full = {"TOGGL_API_TOKEN": "abc", "TOGGL_ORGANIZATION_ID": "1", "TOGGL_WORKSPACE_ID": "2"}
    assert timelog.load_toggl(env={}) is None
    assert timelog.load_toggl(env={"TOGGL_API_TOKEN": "abc"}) is None   # ID がないと宛先が決まらない
    assert timelog.load_toggl(env=full | {"TOGGL_WORKSPACE_ID": "x"}) is None
    toggl = timelog.load_toggl(env=full)
    assert isinstance(toggl, timelog.Toggl)
    assert (toggl.organization_id, toggl.workspace_id) == (1, 2)
