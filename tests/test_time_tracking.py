from __future__ import annotations

from kei_agent.store import Store
from kei_agent.time_tracking import TimerContext, TimeTracker


def _context(domain: str, channel: str, *, course_page_id: str = "", course_name: str = "") -> TimerContext:
    return TimerContext(
        user_id="U1",
        domain=domain,
        channel_id=channel,
        channel_name=channel.lower(),
        course_page_id=course_page_id,
        course_name=course_name,
    )


def test_starting_another_timer_finishes_the_previous_one(store):
    tracker = TimeTracker(store)
    first, replaced = tracker.start(_context("research", "C10"), started_at=100.0)
    second, replaced = tracker.start(_context("work", "C30"), started_at=130.0)

    assert replaced is not None
    assert replaced.id == first.id
    assert replaced.ended_at == 130.0
    assert tracker.active("U1") is not None
    assert tracker.active("U1").id == second.id


def test_reopening_store_restores_unfinished_timer(tmp_path):
    path = tmp_path / "state.db"
    tracker = TimeTracker(Store(path))
    entry, _ = tracker.start(_context("course", "C20"), started_at=100.0)

    restored = TimeTracker(Store(path)).active("U1")

    assert restored is not None
    assert restored.id == entry.id
    assert restored.ended_at is None


def test_work_entry_also_goes_to_notion_and_memo_is_trimmed(store):
    """仕事の時間も、研究・大学と同じ共通ホームの時間記録に書く。"""
    tracker = TimeTracker(store)
    entry, _ = tracker.start(_context("work", "C30"), started_at=100.0)

    updated = tracker.add_memo(entry.id, "  企画の整理  ")

    assert updated.notion_state == "pending"
    assert updated.memo == "企画の整理"


def test_channel_binding_keeps_course_identity_when_name_changes(store):
    tracker = TimeTracker(store)
    tracker.bind_course_channel("C21", "course-page", "マルチメディア工学A")

    binding = tracker.course_binding("C21")

    assert binding is not None
    assert binding.course_page_id == "course-page"
    assert binding.course_name == "マルチメディア工学A"
