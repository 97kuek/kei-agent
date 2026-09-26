"""provider 別 session の migration と切替境界。"""

from kei_agent.store import Store


def test_legacy_session_is_preserved_but_not_resumed(store):
    store.upsert_thread("C", "1", "research", "old-id")
    assert store.session_for("C", "1", "research", "codex", "v2") is None
    assert store.get_thread("C", "1")["session_id"] == "old-id"


def test_provider_switch_cannot_resume_old_provider_session(store):
    store.set_session("C", "1", "research", "claude", "claude-id", "v1")
    assert store.session_for("C", "1", "research", "claude", "v1") == "claude-id"
    store.set_session("C", "1", "research", "codex", "codex-id", "v1")
    assert store.session_for("C", "1", "research", "claude", "v1") is None
    assert store.last_provider("C", "1", "research") == "codex"
    assert store.session_for("C", "1", "research", "codex", "v2") is None


def test_provider_session_survives_restart(store):
    store.set_session("C", "1", "course", "codex", "new-id", "v1")
    reopened = Store(store.path)
    assert reopened.session_for("C", "1", "course", "codex", "v1") == "new-id"


def test_provider_limit_survives_restart_without_blocking_the_other_provider(store):
    store.set_limit_until("claude", 1000.0)
    reopened = Store(store.path)
    assert reopened.limit_until("claude") == 1000.0
    assert reopened.limit_until("codex") == 0.0


def test_nightly_pruning_drops_old_records_but_keeps_what_aggregation_needs(store):
    import time as _time

    from kei_agent.store import RUNS_MIN_DAYS

    now = _time.time()
    old = now - 100 * 86400
    week_ago = now - 7 * 86400
    store.set_session("C1", "1.1", "research", "claude", "sess-old", "v1")
    store.set_session("C1", "2.2", "research", "claude", "sess-new", "v1")
    old_deferred = store.defer_run("request", {}, 0)
    store.finish_deferred(old_deferred)
    store.defer_run("request", {"pending": True}, 0)
    store.record_schedule("review", "2026-06-01")
    store.record_schedule("daily", "2026-06-01")
    store.record_schedule("daily", "2026-09-24")
    old_run = store.start_run("C1", "1.1", "vlm", "message")
    recent_run = store.start_run("C1", "2.2", "vlm", "message")
    store.end_run(old_run, False, None)
    store.end_run(recent_run, False, None)
    with store.conn:
        store.conn.execute("UPDATE provider_sessions SET updated_at = ? WHERE thread_ts = '1.1'", (old,))
        store.conn.execute("UPDATE provider_thread_state SET updated_at = ? WHERE thread_ts = '1.1'", (old,))
        store.conn.execute("UPDATE deferred_runs SET created_at = ?", (old,))
        store.conn.execute("UPDATE schedule_runs SET ran_at = ? WHERE day = '2026-06-01'", (old,))
        store.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?", (old, old + 60, old_run))
        store.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?",
                           (now - (RUNS_MIN_DAYS - 7) * 86400, week_ago, recent_run))

    # 朝の読みものは、👍 したものだけ残す（次からの参考と、外したときに Notion から消すため）
    store.add_reading_post("C40", "1.1", "2026-06-01", {"title": "見ただけ"})
    store.add_reading_post("C40", "2.2", "2026-06-01", {"title": "👍 した"})
    store.set_reading_like("C40", "2.2", old, "page-1")
    with store.conn:
        store.conn.execute("UPDATE reading_posts SET posted_at = ?", (old,))

    # 保持を短く（7日）しても、runs は 8 週ぶん残す
    assert store.drop_old_agent_sessions(week_ago) > 0

    assert store.reading_post("C40", "1.1") is None and store.reading_post("C40", "2.2")["notion_page_id"] == "page-1"

    assert store.session_for("C1", "1.1", "research", "claude", "v1") is None
    assert store.session_for("C1", "2.2", "research", "claude", "v1") == "sess-new"
    assert [payload for _, payload in store.pending_deferred("request")] == [{"pending": True}]
    assert store.last_schedule("review") is not None           # 処理ごとの最新は残す
    assert not store.schedule_ran("daily", "2026-06-01") and store.schedule_ran("daily", "2026-09-24")
    ids = [r["id"] for r in store.conn.execute("SELECT id FROM runs")]
    assert ids == [recent_run]
