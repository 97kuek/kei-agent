"""モジュールの枠の広がり（段階3の Daily・振り返りの①）。Daily と振り返りを載せ替える前に、利用者のモジュールで確かめる。

本体の定期処理を受け持つ（core_schedules）、朝の一覧（core.morning）、材料（core.digest）、研究全体を読んで AI を動かす
（core.run_ai の overview）、見出しとスレッドに出す（core.publish）。
"""

from dataclasses import replace
from datetime import datetime

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack, write_config

from kei_agent import modules, runner
from kei_agent.assistant import Assistant
from kei_agent.configuration.config import ConfigError, load_config
from kei_agent.jobs import JobManager
from kei_agent.request import Request
from kei_agent.schedule import Scheduler, task_names
from kei_agent.themes import ChannelKind

BRIEF_TOML = '''api = 1
name = "brief"
label = "まとめ"
core_schedules = ["daily", "review"]

[actor]
prompt = "brief.md"
files = "read"
shell = false
web = false
default_use_case = "brief_write"

[use_cases.brief_write]
offline = true
claude = { model = "claude-sonnet-5", effort = "medium" }
'''

BRIEF_CODE = '''from kei_agent.api import AIError, Core, final_answer


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.seen = {}

    async def run_schedule(self, name, day):
        from datetime import datetime
        import time

        ids = await self.core.channel_ids()
        channel = ids[self.core.channels("overview")[0]]
        failed = await self.core.gather_prepare(name, day)
        material = await self.core.digest(time.time() - 86400, time.time(), f"{name} の材料", agenda=name == "review")
        self.seen[name] = {"material": material, "failed": failed}
        try:
            text = await self.core.run_ai("brief_write", material, overview=True, trigger=name)
        except AIError as e:
            return {"status": "error", "error": str(e)}
        header = name
        if name == "daily":
            morning = await self.core.morning(datetime.now())
            header = morning.text
        thread_ts = await self.core.publish(channel, header, final_answer(text))
        if name == "daily":
            self.core.mark_shown(morning.notices)
        return {"status": "posted", "thread_ts": thread_ts}
'''


def _brief(root, toml=BRIEF_TOML, code=BRIEF_CODE):
    folder = root / "brief"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text(toml, encoding="utf-8")
    if code is not None:
        (folder / "module.py").write_text(code, encoding="utf-8")
    (folder / "brief.md").write_text("# まとめ\n", encoding="utf-8")
    return folder


class RecordingClaude(FakeClaude):
    async def __call__(self, config, request, prompt, on_activity=None):
        result = await super().__call__(config, request, prompt, on_activity)
        self.calls[-1].update(actor=request.recipe.actor, use_case=str(request.recipe.use_case),
                              kind=request.workspace.kind, read_only=request.read_only)
        return result


@pytest.fixture
def env(config, store, tmp_path, monkeypatch):
    modules.register_user_modules(_brief(tmp_path / "user-modules").parent)
    # 組み込みの Daily・振り返りは外す（本体の定期処理を受け持てるのは1つだけ）
    config = replace(config, modules=(*[name for name in config.modules if name != "daily"], "brief"),
                     agent_profiles={**config.agent_profiles, "brief": config.agent_profiles["work"]})
    slack = FakeSlack({"C5": "0-overview", "C9": "0-kei-agent"})
    claude = RecordingClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack, claude




# 本体の定期処理を受け持つ

def test_core_schedules_are_checked(tmp_path):
    folder = _brief(tmp_path, BRIEF_TOML.replace('core_schedules = ["daily", "review"]', 'core_schedules = ["night"]'))
    with pytest.raises(modules.ModuleError, match="受け持てない本体の定期処理"):
        modules.load_spec(folder)
    folder = _brief(tmp_path / "b", code="class Module:\n    def __init__(self, core):\n        pass\n")
    with pytest.raises(modules.ModuleError, match="run_schedule"):
        modules.load_code(modules.load_spec(folder))
    # 受け持てるのは1つだけ
    home = tmp_path / "home"
    home.mkdir()
    write_config(home / "config.toml", 'modules = ["brief", "brief2"]\n')
    _brief(home / "modules")
    other = _brief(home / "modules" / "x", BRIEF_TOML.replace('name = "brief"', 'name = "brief2"')
                   .replace("brief_write", "brief2_write"))
    other.rename(home / "modules" / "brief2")
    with pytest.raises(ConfigError, match="どちらも本体の定期処理（daily）"):
        load_config(env={"KEI_AGENT_HOME": str(home)})


async def test_the_scheduler_hands_daily_and_review_to_the_module(env, config):
    scheduler, assistant, slack, claude = env
    # 順番も時刻の書き方も、今までどおり（Daily と振り返りは、モジュールの定期処理のあと）
    assert task_names(config)[-3:] == ("daily", "review", "maintenance")
    assert scheduler.task_provider("daily") == "claude"
    core = assistant.cores["brief"]
    # モジュールからも研究全体・改善のチャンネルが分かる
    assert core.channels("overview") == config.overview_channels
    assert core.channels("improve") == config.improve_channels
    assert core.last_ran("daily") is None

    detail = await scheduler.run_task("daily", "2026-09-28")

    assert detail["status"] == "posted"
    call, = claude.calls
    # 研究全体の作業場で、読むだけで動かす（研究テーマのフォルダとスレッドの記録を読める）
    assert (call["actor"], call["use_case"], call["kind"], call["read_only"]) == (
        "brief", "brief_write", ChannelKind.OVERVIEW, True)
    assert call["cwd"] == config.overview_dir
    header, body = slack.posted()[:2]
    assert header["channel"] == "C5" and header["text"].startswith("☀️")         # 朝の一覧が見出し
    assert body["thread_ts"] == detail["thread_ts"] and body["markdown_text"] == "結果です"
    # 返信を拾えるようにスレッドを覚え、本文をスレッドの記録にも残す
    assert assistant.store.get_thread("C5", detail["thread_ts"]) is not None
    log = config.overview_dir / ".kei-agent" / "threads" / f"{detail['thread_ts']}.md"
    assert "結果です" in log.read_text(encoding="utf-8")
    # Kei Agent の稼働として、定期処理の名前で記録する
    run, = assistant.store.conn.execute("SELECT channel_name, trigger FROM runs").fetchall()
    assert (run["channel_name"], run["trigger"]) == ("overview", "daily")
    # 最後に動いた回が分かる（その日より前の回だけを聞くこともできる）
    ran = core.last_ran("daily")
    assert ran is not None and core.last_ran("daily", before_day="2026-09-29") == ran
    assert core.last_ran("daily", before_day="2026-09-28") is None


async def test_the_digest_holds_the_core_records_and_other_modules_material(env):
    scheduler, assistant, slack, claude = env
    assistant.hub.minutes = {"研究": 60}
    await scheduler.run_task("review", "2026-09-28")
    material = assistant.modules["brief"].seen["review"]["material"]
    assert material.startswith("# review の材料")
    for section in ("## やり取りのあったスレッド", "## 終わったジョブ", "## 夜間の Task", "## Kei Agent の稼働（今週）",
                    "## 時間（今週）"):
        assert section in material


async def test_a_limit_pauses_the_schedules_and_is_reported_as_an_error(env):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"is_error": True, "text": "", "errors": ["usage limit reached|1999999999"]}]
    detail = await scheduler.run_task("daily", "2026-09-28")
    assert detail["status"] == "error"
    run, = assistant.store.conn.execute("SELECT ended_at, is_error FROM runs").fetchall()
    assert run["ended_at"] is not None and run["is_error"]


async def test_the_morning_list_marks_its_notices_only_when_asked(env, monkeypatch):
    scheduler, assistant, slack, claude = env
    core = assistant.cores["brief"]

    async def agenda(days, kinds=None):
        return {"course": [{"kind": "due", "title": "レポート", "at": datetime.now().replace(hour=23, minute=59)
                            .isoformat(timespec="minutes"), "notice": "due:1"}]}, []

    monkeypatch.setattr(assistant, "module_agenda", agenda)
    morning = await core.morning(datetime.now())
    assert "レポート" in morning.text and morning.notices == ("module.course.due:1",)
    assert not assistant.store.noticed("module.course.due:1")
    core.mark_shown(morning.notices)
    assert assistant.store.noticed("module.course.due:1")


async def test_run_ai_keeps_the_request_thread_informed(env):
    """req を渡した回は、そのスレッドの経過と答えを見せる（Daily のように、スレッドを持たない回は見せない）。"""
    scheduler, assistant, slack, claude = env
    text = await assistant.cores["brief"].run_ai("brief_write", "まとめて", req=Request("C9", "kei-agent", "7.1", "7.1", "x"))
    assert "結果です" in text and slack.streamed()[-1] == "結果です"
