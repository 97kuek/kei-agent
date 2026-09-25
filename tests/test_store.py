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
