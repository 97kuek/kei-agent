"""大学のモジュール（modules/course/）。本体の差し込み口（招待・取り込み・見回り）と、載せ替えのときの移し替え。"""

import json
from datetime import datetime

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack

from kei_agent.conversation.assistant import Assistant
from kei_agent.execution import runner
from kei_agent.execution.jobs import JobManager
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "0-overview", "C7": "2-course", "C9": "0-kei-agent"})
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
        from kei_agent.conversation import a2a
        self.asked.append(skill)
        return a2a.TaskResult(state="TASK_STATE_COMPLETED" if self.ok else "TASK_STATE_FAILED", text=json.dumps(
            {"ok": self.ok, "text": "済", "data": {"added": self.added, "updated": self.updated},
             "limit_reset_at": None, "cost_usd": None}))


async def test_joining_the_course_channel_introduces_the_module(env, config):
    """#2-course に招かれたら、大学のモジュールの案内を出す（研究テーマにはしない）。"""
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


async def test_the_course_module_ticks_hourly(env, monkeypatch):
    """見回りは毎分呼ばれるが、締切を見に行くのは1時間に1回。"""
    from kei_agent.conversation.agents import Reply

    scheduler, assistant, slack = env
    module = assistant.modules["course"]
    calls = []

    async def ask_agent(skill, payload):
        calls.append(payload.get("days"))
        return Reply(ok=True, data={"items": []})

    async def sync_calendar(now):
        return None

    monkeypatch.setattr(module.core, "ask_agent", ask_agent)
    monkeypatch.setattr(module, "sync_calendar", sync_calendar)
    await scheduler.module_ticks(datetime(2026, 9, 28, 9, 0))
    await scheduler.module_ticks(datetime(2026, 9, 28, 9, 1))
    await scheduler.module_ticks(datetime(2026, 9, 28, 10, 1))
    assert calls == [4, 4]


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
