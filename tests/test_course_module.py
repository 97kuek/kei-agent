"""大学のモジュール（modules/course/）。本体の差し込み口（招待・取り込み・見回り）と、載せ替えのときの移し替え。"""

import json
from datetime import datetime

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack

from kei_agent import modules, runner
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager
from kei_agent.schedule import Scheduler
from kei_agent.store import Store


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "01_overview", "C7": "20_course", "C9": "00_kei-agent"})
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
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
        from kei_agent import a2a
        self.asked.append(skill)
        return a2a.TaskResult(state="TASK_STATE_COMPLETED" if self.ok else "TASK_STATE_FAILED", text=json.dumps(
            {"ok": self.ok, "text": "済", "data": {"added": self.added, "updated": self.updated},
             "limit_reset_at": None, "cost_usd": None}))


async def test_joining_the_course_channel_introduces_the_module(env, config):
    """#20_course に招かれたら、大学のモジュールの案内を出す（研究テーマにはしない）。"""
    scheduler, assistant, slack = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C7"})
    text, = slack.texts()
    assert text.startswith("Kei Agent です。このチャンネルの用事は大学エージェントに取り次ぎます。")
    assert "課題を取り込んで" in text and assistant.notion.themes == {}
    assert assistant.default_question("course") == "授業について教えて"


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


async def test_new_and_changed_assignments_are_posted_with_a_count_heading(env):
    """見出しに件数をまとめ、行頭のラベルは締切変更だけに :repeat: を付ける。"""
    scheduler, assistant, slack = env
    assistant.agents["course"] = FakeCourseAgent(
        added=["`10/02 14:20-14:50` マルチメディア工学Ｂ / Short test 1"],
        updated=["`10/05 23:59` データベース / 第3回レポート"])
    module = assistant.modules["course"]

    assert await module.sync_assignments() is True

    text, = slack.texts()
    assert text == (
        "📚 Moodle の課題（新着 1件・締切変更 1件）\n"
        "• `10/02 14:20-14:50` マルチメディア工学Ｂ / Short test 1\n"
        "• :repeat: `10/05 23:59` データベース / 第3回レポート")


async def test_only_changed_assignments_do_not_show_zero_new(env):
    """締切変更だけの回は、見出しに「新着 0件」を出さない。"""
    scheduler, assistant, slack = env
    assistant.agents["course"] = FakeCourseAgent(updated=["`10/05 23:59` データベース / 第3回レポート"])

    assert await assistant.modules["course"].sync_assignments() is True

    text, = slack.texts()
    assert text.splitlines()[0] == "📚 Moodle の課題（締切変更 1件）"


async def test_the_course_module_ticks_hourly(env, monkeypatch):
    """見回りは毎分呼ばれるが、締切を見に行くのは1時間に1回。"""
    scheduler, assistant, slack = env
    module = assistant.modules["course"]
    calls = []

    async def dues(days):
        calls.append(days)
        return []

    async def unstarted(channel, now):
        return None

    monkeypatch.setattr(module, "dues", dues)
    monkeypatch.setattr(module, "notify_unstarted", unstarted)
    await scheduler.module_ticks(datetime(2026, 9, 28, 9, 0))
    await scheduler.module_ticks(datetime(2026, 9, 28, 9, 1))
    await scheduler.module_ticks(datetime(2026, 9, 28, 10, 1))
    assert calls == [2, 2]


def test_old_deadline_notices_move_to_the_course_module(tmp_path):
    """本体が持っていた知らせの目印（due: / early:）は、大学のモジュールの目印に移す（二重に知らせない）。"""
    path = tmp_path / "state.db"
    store = Store(path)
    store.record_notice("due:1@moodle:2026-10-25T23:59:00+09:00")
    store.record_notice("early:a:2026-10-26")
    store.record_notice("version:abc")
    store.conn.commit()

    store = Store(path)

    assert store.noticed("module.course.due:1@moodle:2026-10-25T23:59:00+09:00")
    assert store.noticed("module.course.early:a:2026-10-26") and store.noticed("version:abc")
    assert not store.noticed("due:1@moodle:2026-10-25T23:59:00+09:00")
    Store(path)     # 2回目は何もしない


def test_the_course_module_reads_its_settings_through_both_windows(env, config):
    """設定は module.toml の [settings] の既定と config.toml の [course]。本体側は core.settings、担当側は settings()。"""
    from kei_agent_a2a.api import settings

    scheduler, assistant, slack = env
    assert assistant.cores["course"].settings == {"school": "", "periods": {}, "terms": {}}
    assert settings(config, "course") == assistant.cores["course"].settings


def test_course_commands_run_through_the_common_command():
    """setup などは `kei-agent-module course <コマンド>` で動く（pyproject.toml にコマンドの名前を持たない）。"""
    commands = modules.load_commands(modules.builtin()["course"])
    assert set(commands) == {"setup", "sync", "inspect", "academic-import"}
