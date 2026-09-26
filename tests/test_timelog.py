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


class FakeToggl:
    def __init__(self, entries):
        self._entries = entries
        self.asked = []

    def entries(self, since, until):
        self.asked.append((since, until))
        return self._entries


class FakeTimeHub:
    def __init__(self, known=()):
        self.known = set(known)
        self.recorded = []

    def time_ids_since(self, since):
        return set(self.known)

    def record_time(self, entry_id, domain, label, started_at, minutes, memo="", slack_url="", source="Slack"):
        self.recorded.append({"id": entry_id, "domain": domain, "label": label, "started_at": started_at,
                              "minutes": minutes, "memo": memo, "source": source})


def test_import_toggl_adds_only_marked_entries_measured_outside_slack():
    """Toggl のアプリで直接測った分だけを「toggl:<id>」で入れる。Slack から送った分と、入れ済みの分は飛ばす。"""
    slack_start = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    toggl = FakeToggl([
        # Slack から送った記録（Toggl 側の開始が数秒ずれても同じとみなす）
        _entry((slack_start + timedelta(seconds=20)).isoformat(), 1500, project="研究 / vlm", id=1),
        _entry("2026-09-21T13:00:00Z", 3000, project="大学/データベース", id=2, description="過去問"),
        _entry("2026-09-21T15:00:00Z", 600, project="アルバイト", id=3),        # 印がない
        _entry("2026-09-21T16:00:00Z", -1, project="研究/vlm", id=4),         # 計測中
        _entry("2026-09-21T17:00:00Z", 900, project="仕事/定例", id=5),        # 入れ済み
        _entry("2026-09-21T18:00:00Z", 900, project="研究/vlm", id=6, type="break"),
        _entry("2026-09-21T19:00:00Z", 30, project="仕事/定例", id=7, description="仕事/定例"),
    ])
    hub = FakeTimeHub(known={"toggl:5"})
    own = [(slack_start.timestamp(), 1500.0)]

    result = timelog.import_toggl(toggl, hub, own, date(2026, 9, 15), date(2026, 9, 21))

    assert toggl.asked == [(date(2026, 9, 15), date(2026, 9, 21))]
    assert [r["id"] for r in hub.recorded] == ["toggl:2", "toggl:7"]
    first = hub.recorded[0]
    assert (first["domain"], first["label"], first["minutes"], first["memo"], first["source"]) == (
        "大学", "データベース", 50, "過去問", "Toggl")
    assert datetime.fromisoformat(first["started_at"]) == datetime(2026, 9, 21, 13, 0, tzinfo=UTC)
    assert datetime.fromisoformat(first["started_at"]).tzinfo is not None
    # 説明がプロジェクト名と同じならメモにしない。1分に満たなくても1分として残す
    assert (hub.recorded[1]["memo"], hub.recorded[1]["minutes"]) == ("", 1)
    assert result == {"status": "done", "imported": 2, "own": 1, "known": 1, "unmarked": 1}


def test_import_toggl_treats_a_long_gap_as_a_different_entry():
    """開始か長さが1分より大きくずれていれば、Slack の記録とは別のものとして入れる。"""
    start = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    toggl = FakeToggl([_entry(start.isoformat(), 1500, project="研究/vlm", id=9)])
    hub = FakeTimeHub()

    timelog.import_toggl(toggl, hub, [(start.timestamp(), 1500.0 + 120)], date(2026, 9, 21), date(2026, 9, 21))

    assert [r["id"] for r in hub.recorded] == ["toggl:9"]


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
