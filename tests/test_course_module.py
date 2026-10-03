"""大学のモジュール（modules/course/）。本体の差し込み口（招待・取り込み・見回り）と、載せ替えのときの移し替え。"""

import json
from datetime import datetime, timedelta

import pytest
from fakes import FakeAI, FakeHub, FakeNotion, make_assistant

from kei_agent.execution import runner
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeAI())
    channels = {"C1": "vlm", "C5": "0-overview", "C7": "2-course", "C9": "0-kei-agent"}
    assistant, slack = make_assistant(config, store, channels,
                                      notion=FakeNotion(), hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack


class FakeCourseAgent:
    """大学の担当の代わり。取り込みの結果だけを返す。"""
    base_url = "http://127.0.0.1:8787"

    def __init__(self, ok=True, added=(), updated=()):
        self.ok = ok
        self.added = list(added)
        self.updated = list(updated)
        self.asked = []

    async def stream(self, skill, text="", params=None, on_progress=None):
        from kei_agent.execution import a2a
        self.asked.append(skill)
        return a2a.TaskResult(state="TASK_STATE_COMPLETED" if self.ok else "TASK_STATE_FAILED", text=json.dumps(
            {"ok": self.ok, "text": "済", "data": {"added": self.added, "updated": self.updated},
             "limit_reset_at": None, "cost_usd": None}))


def test_the_course_channel_routes_to_the_course_module(env):
    """大学のチャンネルは、研究テーマの作業場にせず大学の担当に取り次ぐ。"""
    from kei_agent.workspaces import themes

    _, assistant, _ = env
    workspace = themes.resolve(assistant.config, "2-course")
    assert (workspace.kind, workspace.module) == (themes.ChannelKind.MODULE, "course")


async def test_the_morning_says_when_assignments_were_not_copied_to_the_calendar(env):
    """昨日までに予定カレンダーへ課題を写せていなければ、朝の一覧の「うまくいかなかったこと」に出す。"""
    scheduler, assistant, slack = env
    assistant.agents["course"] = FakeCourseAgent()
    module = assistant.modules["course"]
    # まだ一度も写していない（載せ替えたばかり）なら、何も言わない
    assert await module.prepare("daily", "2026-09-28") == []
    module.core.records.put("calendar", "synced", {"day": "2026-09-26"})
    assert await module.prepare("daily", "2026-09-28") == ["予定カレンダーへの課題の書き込み"]
    module.core.records.put("calendar", "synced", {"day": "2026-09-27"})
    assert await module.prepare("daily", "2026-09-28") == []
    # 取り込めなかったことも伝える
    assistant.agents["course"] = FakeCourseAgent(ok=False)
    assert await module.prepare("review", "2026-09-28") == ["課題の取り込み"]


MM = "`10/02 14:20-14:50` マルチメディア工学Ｂ / Short test 1"
DB = "`10/05 23:59` データベース / 第3回レポート"


@pytest.mark.parametrize(("added", "updated", "text"), [
    ([MM], [DB], f"📚 Moodle の課題（新着 1件・締切変更 1件）\n• {MM}\n• :repeat: {DB}"),
    ([], [DB], f"📚 Moodle の課題（締切変更 1件）\n• :repeat: {DB}"),   # 「新着 0件」は出さない
])
async def test_new_and_changed_assignments_are_posted_with_a_count_heading(env, added, updated, text):
    """見出しに件数をまとめ、行頭のラベルは締切変更だけに :repeat: を付ける。"""
    scheduler, assistant, slack = env
    assistant.agents["course"] = FakeCourseAgent(added=added, updated=updated)

    assert await assistant.modules["course"].sync_assignments() is True
    assert slack.texts() == [text]


async def test_ticks_check_submissions_every_ten_minutes_without_deadline_notifications(env, monkeypatch):
    """提出状態は機械的に確認し、締切通知は Dot に統一する。"""
    scheduler, assistant, slack = env
    module = assistant.modules["course"]
    calls = []
    checked = []

    async def ask_agent(skill, payload):
        from kei_agent.api import Reply

        assert skill in {"sync-assignments", "sync-submissions"}
        checked.append(skill)
        return Reply(ok=True, text="未設定", data={"enabled": False})

    async def sync_calendar(now, *, force=False):
        calls.append(now)

    monkeypatch.setattr(module.core, "ask_agent", ask_agent)
    monkeypatch.setattr(module, "sync_calendar", sync_calendar)
    await scheduler.module_ticks(datetime(2026, 9, 28, 9, 0))
    await scheduler.module_ticks(datetime(2026, 9, 28, 9, 1))
    await scheduler.module_ticks(datetime(2026, 9, 28, 10, 1))
    assert len(calls) == 3 and slack.posted() == []
    assert checked == ["sync-assignments", "sync-assignments"]


def test_the_course_module_reads_its_settings_through_both_windows(env, config):
    """設定は module.toml の [settings] の既定と config.toml の [course]。本体側は core.settings、担当側は settings()。"""
    from kei_agent_a2a.api import settings

    scheduler, assistant, slack = env
    assert assistant.cores["course"].settings == {"school": "", "periods": {}, "terms": {}}
    assert settings(config, "course") == assistant.cores["course"].settings


def test_course_commands_run_through_the_common_command():
    """setup などは `kei-agent-module course <コマンド>` で動く（pyproject.toml にコマンドの名前を持たない）。"""
    commands = modules.load_commands(modules.builtin()["course"])
    assert set(commands) == {"setup", "sync", "sync-submissions", "inspect", "academic-import"}


async def test_submission_change_refreshes_calendar_on_the_same_day_and_keeps_concurrent_change(env, monkeypatch):
    from kei_agent.api import Reply

    _, assistant, _ = env
    module = assistant.modules["course"]
    now = datetime(2026, 9, 28, 9, 10)
    module._calendar_tried = datetime(2026, 9, 28, 9).timestamp()
    module.core.records.put("calendar", "synced", {"day": "2026-09-28"})
    module.core.records.put("calendar", "dirty", {"dirty": True, "version": "first"})
    calls = []

    async def ask(skill, params):
        assert skill == "list-calendar-assignments"
        return Reply(data={"complete": True, "items": []})

    async def write(*args, **kwargs):
        calls.append(args)
        module.core.records.put("calendar", "dirty", {"dirty": True, "version": "second"})
        return {"updated": 1}

    monkeypatch.setattr(module.core, "ask_agent", ask)
    monkeypatch.setattr(module.core, "sync_calendar", write)
    await module.sync_calendar(now)
    assert len(calls) == 1
    assert module.core.records.get("calendar", "dirty")["version"] == "second"


async def test_moodle_refreshes_after_start_and_resume_without_reimporting_each_tick(env, monkeypatch):
    """起動・復帰で取り込み、稼働中は締切30分・提出10分だけ同期する。"""
    from kei_agent.api import Reply
    _, assistant, _ = env
    module = assistant.modules["course"]
    calls = []

    async def ask(skill, params):
        calls.append(skill)
        return Reply(ok=True, data={})

    monkeypatch.setattr(module.core, "ask_agent", ask)
    assistant.hub = None
    start = datetime(2026, 9, 28, 9)
    for minute in range(32):
        await module.tick(start + timedelta(minutes=minute))
    assert calls == ["sync-assignments", "sync-submissions", "sync-submissions", "sync-assignments"]
    assert module.core.records.get("moodle", "synced")["at"] == "2026-09-28T09:30:00"
    await module.tick(start + timedelta(minutes=37))
    await module.tick(start + timedelta(minutes=38))
    assert calls == ["sync-assignments", "sync-submissions", "sync-submissions", "sync-assignments", "sync-assignments"]


@pytest.mark.parametrize("failure", ["transport", "partial", "exception"])
async def test_failed_sync_retries_without_recording_success_or_repeating_submissions(env, monkeypatch, failure):
    """失敗時は成功時刻を進めず、毎分や二重の提出確認を避けて再試行する。"""
    from kei_agent.api import Reply
    _, assistant, _ = env
    module = assistant.modules["course"]
    calls = []

    async def ask(skill, params):
        calls.append(skill)
        if failure == "exception" and len(calls) == 1:
            raise RuntimeError("一時的な接続失敗")
        return Reply(ok=failure != "transport" or len(calls) > 1,
                     data={"errors": ["一部失敗"] if failure == "partial" and len(calls) == 1 else []})

    monkeypatch.setattr(module.core, "ask_agent", ask)
    assistant.hub = None
    module.core.records.put("moodle", "synced", {"at": "2026-09-27T09:00:00"})
    start = datetime(2026, 9, 28, 9)
    if failure == "exception":
        with pytest.raises(RuntimeError):
            await module.tick(start)
    else:
        await module.tick(start)
    for minute in range(1, 10):
        await module.tick(start + timedelta(minutes=minute))
    assert calls == ["sync-assignments"]
    assert module.core.records.get("moodle", "synced")["at"] == "2026-09-27T09:00:00"
    await module.tick(start + timedelta(minutes=10))
    assert calls == ["sync-assignments", "sync-assignments"]
    assert module.core.records.get("moodle", "synced")["at"] == "2026-09-28T09:10:00"


async def test_slow_sync_is_not_mistaken_for_sleep_on_the_next_tick(env, monkeypatch):
    """取り込みの所要時間だけ空いても、復帰としてすぐ取り込み直さない。"""
    from kei_agent.api import Reply
    _, assistant, _ = env
    module = assistant.modules["course"]
    calls = []
    clocks = iter([0, 360, 420, 420])
    monkeypatch.setattr(__import__(type(module).__module__, fromlist=["monotonic"]),
                        "monotonic", lambda: next(clocks), raising=False)

    async def ask(skill, params):
        calls.append(skill)
        return Reply(ok=True, data={})

    monkeypatch.setattr(module.core, "ask_agent", ask)
    assistant.hub = None
    await module.tick(datetime(2026, 9, 28, 9))
    await module.tick(datetime(2026, 9, 28, 9, 7))
    assert calls == ["sync-assignments"]


async def test_prepare_and_tick_share_an_inflight_import(env, monkeypatch):
    """朝の取り込みと起動直後の見回りが重なっても、全同期と通知は一度だけ。"""
    import asyncio

    from kei_agent.api import Reply

    _, assistant, slack = env
    module = assistant.modules["course"]
    assistant.hub = None
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def ask(skill, params):
        calls.append(skill)
        started.set()
        await release.wait()
        return Reply(ok=True, data={"added": ["テスト課題"]})

    monkeypatch.setattr(module.core, "ask_agent", ask)
    preparation = asyncio.create_task(module.prepare("daily", "2026-09-28"))
    await started.wait()
    tick = asyncio.create_task(module.tick(datetime(2026, 9, 28, 9)))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(preparation, tick)
    assert calls == ["sync-assignments"]
    assert len(slack.texts()) == 1


async def test_submission_failures_preserve_the_last_success_and_retry_after_ten_minutes(env, monkeypatch):
    """提出同期だけの部分失敗でも成功時刻を保持し、締切取り込みとは別に再試行する。"""
    from kei_agent.api import Reply

    _, assistant, _ = env
    module = assistant.modules["course"]
    assistant.hub = None
    attempts = 0

    async def ask(skill, params):
        nonlocal attempts
        if skill == "sync-submissions":
            attempts += 1
        return Reply(ok=True, data={"enabled": True, "submissions_enabled": True,
                                    "errors": ["一部失敗"] if attempts == 1 else []})

    monkeypatch.setattr(module.core, "ask_agent", ask)
    start = datetime(2026, 9, 28, 9)
    for minute in range(20):
        await module.tick(start + timedelta(minutes=minute))
    assert module.core.records.get("moodle", "synced")["at"] == "2026-09-28T09:00:00"
    assert module.core.records.get("moodle", "submissions_synced")["at"] == "2026-09-28T09:00:00"
    await module.tick(start + timedelta(minutes=20))
    assert module.core.records.get("moodle", "submissions_synced")["at"] == "2026-09-28T09:20:00"
    assert attempts == 2


async def test_course_questions_only_guide_to_dot_without_ai_or_routine_execution(env):
    """大学の質問を Mac の AI や定型処理に渡さず、Dot の資料参照を案内する。"""
    from kei_agent.api import Request

    _, assistant, slack = env
    module = assistant.modules["course"]
    agent = assistant.agents["course"] = FakeCourseAgent()
    for question, skill in [("単位の取り方を相談したい", "ask"), ("課題を取り込んで", "sync-assignments")]:
        await module.on_message(Request("C7", "2-course", "11.1", "11.1", question), skill=skill)
    assert agent.asked == []
    assert len(slack.texts()) == 2
    assert all("Dot" in text and "Notion" in text and "Box" in text for text in slack.texts())


@pytest.mark.parametrize("previous", [None, {"at": "2026-09-27T09:00:00"}])
async def test_unconfigured_submission_api_does_not_record_submission_success(env, monkeypatch, previous):
    """API が未設定でも締切は取り込み、提出の成功記録は作成・更新しない。"""
    from kei_agent.api import Reply

    _, assistant, _ = env
    module = assistant.modules["course"]
    assistant.hub = None
    calls = []

    async def ask(skill, params):
        calls.append(skill)
        data = {"submissions_enabled": False} if skill == "sync-assignments" else {"enabled": False}
        return Reply(ok=True, data=data)

    monkeypatch.setattr(module.core, "ask_agent", ask)
    if previous:
        module.core.records.put("moodle", "submissions_synced", previous)
    start = datetime(2026, 9, 28, 9)
    for minute in range(11):
        await module.tick(start + timedelta(minutes=minute))
        assert module.core.records.get("moodle", "submissions_synced") == previous
    assert module.core.records.get("moodle", "synced")["at"] == "2026-09-28T09:00:00"
    assert calls == ["sync-assignments", "sync-submissions"]
