"""モジュールの枠の広がり（段階3の時間記録の①）。スラッシュコマンド、投稿のボタンと入力の画面、ほかのモジュールへの
頼みごと、Daily と振り返りの材料。時間記録を載せ替える前に、利用者のモジュールで確かめる。
"""

import json
from dataclasses import replace

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent.configuration.config import ConfigError, load_config
from kei_agent.conversation.assistant import Assistant
from kei_agent.execution import a2a, runner
from kei_agent.execution.jobs import JobManager
from kei_agent.framework import modules
from kei_agent.scheduling.digest import DigestBuilder

STAMP_TOML = '''api = 1
name = "stamp"
label = "スタンプ"

[depends]
optional = ["course"]

[slash_commands]
stamp = "スタンプを押す"
'''

STAMP_CODE = '''from kei_agent.api import Core


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.pressed = []

    async def on_slash_command(self, name, body):
        ts = await self.core.post(body["channel_id"], "スタンプ", blocks=[{"type": "actions", "elements": [
            {"type": "button", "action_id": self.core.action_id("press"), "value": "1",
             "text": {"type": "plain_text", "text": "押す"}}]}])
        self.core.records.put("card", body["channel_id"], {"ts": ts})
        return f"/{name} を受け付けたよ（{await self.core.channel_name(body['channel_id'])}）"

    async def on_action(self, name, body):
        self.pressed.append(name)
        card = self.core.records.get("card", body["channel"]["id"])
        await self.core.update(body["channel"]["id"], card["ts"], "押された")
        await self.core.open_view(body["trigger_id"], {"type": "modal", "callback_id": self.core.view_id("memo"),
                                                       "blocks": [{"type": "input", "block_id": "memo"}]})

    async def on_view(self, name, body):
        memo = body["view"]["state"]["values"]["memo"]["text"]["value"]
        return {"memo": "空です"} if not memo else None

    async def material(self, now):
        return ["", "## スタンプ", "- 今週 3 回"]
'''


def _stamp(root, toml=STAMP_TOML, code=STAMP_CODE):
    folder = root / "stamp"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text(toml, encoding="utf-8")
    if code is not None:
        (folder / "module.py").write_text(code, encoding="utf-8")
    return folder


@pytest.fixture
def env(config, store, tmp_path, monkeypatch):
    modules.register_user_modules(_stamp(tmp_path / "user-modules").parent)
    config = replace(config, modules=(*config.modules, "stamp"))
    slack = FakeSlack({"C1": "1-vlm"})
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    return assistant, slack


async def test_a_module_answers_its_slash_command_and_its_buttons(env):
    assistant, slack = env
    reply = await assistant.module_slash("stamp", {"user_id": "UME", "channel_id": "C1"})
    assert reply == "/stamp を受け付けたよ（1-vlm）"
    button = slack.posted()[-1]["blocks"][0]["elements"][0]
    assert button["action_id"] == "kei_agent_module:stamp:press"

    await assistant.module_action({"user": {"id": "UME"}, "trigger_id": "t", "channel": {"id": "C1"},
                                   "actions": [button]})
    assert assistant.modules["stamp"].pressed == ["press"]
    assert any(name == "chat_update" and kw["text"] == "押された" for name, kw in slack.calls)
    (_, opened), = [(n, kw) for n, kw in slack.calls if n == "views_open"]
    assert opened["view"]["callback_id"] == "kei_agent_module:stamp:memo"

    def sent(memo, user="UME"):
        return {"user": {"id": user}, "view": {"callback_id": "kei_agent_module:stamp:memo",
                                               "blocks": [{"block_id": "memo"}],
                                               "state": {"values": {"memo": {"text": {"value": memo}}}}}}

    assert await assistant.module_view(sent("")) == {"memo": "空です"}
    assert await assistant.module_view(sent("書いた")) is None
    assert await assistant.module_view(sent("書いた", user="USOMEONE")) == {"memo": "依頼者だけが使えます"}


async def test_only_the_owner_uses_module_commands_and_buttons(env):
    assistant, slack = env
    assert await assistant.module_slash("stamp", {"user_id": "USOMEONE", "channel_id": "C1"}) == "この操作は利用できません"
    await assistant.module_action({"user": {"id": "USOMEONE"}, "trigger_id": "t", "channel": {"id": "C1"},
                                   "actions": [{"action_id": "kei_agent_module:stamp:press"}]})
    assert assistant.modules["stamp"].pressed == []


async def test_modules_add_lines_to_the_daily_material(env, config, store):
    assistant, slack = env
    text = await DigestBuilder(assistant.config, store, assistant).build(0, 86400, "Daily", set())
    assert "## スタンプ\n- 今週 3 回" in text


async def test_a_module_may_ask_only_the_modules_it_depends_on(env):
    """ほかのモジュールに頼めるのは、[depends] に書いた相手だけ（大学の科目を聞く時間記録など）。"""
    assistant, slack = env

    class Course:
        base_url = "http://127.0.0.1:8787"

        async def stream(self, skill, text="", params=None, on_progress=None):
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps(
                {"ok": True, "text": "済", "data": {"items": [{"id": "c1", "subject": "データベース"}]},
                 "limit_reset_at": None, "cost_usd": None}, ensure_ascii=False))

    assistant.agents["course"] = Course()
    core = assistant.cores["stamp"]
    reply = await core.ask_module("course", "list-current-courses", {})
    assert reply.ok and reply.data["items"][0]["subject"] == "データベース"
    with pytest.raises(ValueError, match="depends"):
        await core.ask_module("work", "list-events", {})


@pytest.mark.parametrize(("toml", "code", "message"), [
    (STAMP_TOML.replace('stamp = "', '"/stamp" = "'), STAMP_CODE, "/ は付けない"),
    (STAMP_TOML, None, "module.py"),
    (STAMP_TOML, "class Module:\n    def __init__(self, core):\n        pass\n", "on_slash_command"),
])
def test_a_broken_slash_command_is_refused(tmp_path, toml, code, message):
    with pytest.raises(modules.ModuleError, match=message):
        folder = _stamp(tmp_path, toml, code)
        modules.load_code(modules.load_spec(folder))


def test_two_modules_cannot_share_a_slash_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    _stamp(home / "modules")
    other = _stamp(home / "modules2", STAMP_TOML.replace('name = "stamp"', 'name = "stamp2"'))
    (home / "modules" / "stamp2").symlink_to(other)
    with pytest.raises(ConfigError, match="スラッシュコマンド「/stamp」"):
        load_config(env={"KEI_AGENT_HOME": str(home)})
