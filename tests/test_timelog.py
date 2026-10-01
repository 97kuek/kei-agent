import time
from datetime import UTC, date, datetime

from kei_agent.scheduling import timelog


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

    # bulk の本文は配列そのもの（{"items": [...]} だと 400 cannot unmarshal object になる）
    assert posted == [(
        "/organizations/7/workspaces/8/time-entries/bulk",
        [{"project_id": 41, "description": "大学 / マルチメディア工学A",
          "start": "2026-09-23T10:00:00+00:00", "duration": 1500, "type": "activity"}],
    )]
    assert isinstance(posted[0][1], list)


def test_load_toggl_needs_a_token_and_ids():
    full = {"TOGGL_API_TOKEN": "abc", "TOGGL_ORGANIZATION_ID": "1", "TOGGL_WORKSPACE_ID": "2"}
    assert timelog.load_toggl(env={}) is None
    assert timelog.load_toggl(env={"TOGGL_API_TOKEN": "abc"}) is None   # ID がないと宛先が決まらない
    assert timelog.load_toggl(env=full | {"TOGGL_WORKSPACE_ID": "x"}) is None
    toggl = timelog.load_toggl(env=full)
    assert isinstance(toggl, timelog.Toggl)
    assert (toggl.organization_id, toggl.workspace_id) == (1, 2)
