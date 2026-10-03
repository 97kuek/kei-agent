"""モジュールの MCP 操作・ほかのモジュールへの依頼・Daily の材料。"""

import json
from dataclasses import replace

import pytest
from fakes import FakeAI, make_assistant

from kei_agent.execution import a2a, runner
from kei_agent.framework import modules
from kei_agent.scheduling.digest import DigestBuilder

STAMP_TOML = '''api = 2
name = "stamp"
label = "スタンプ"

[depends]
optional = ["course"]
'''

STAMP_CODE = '''from kei_agent.api import Core


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.pressed = []

    async def head_action(self, name, params):
        channel = params.get("channel", "C1")
        if name == "stamp":
            ts = await self.core.post(channel, "スタンプ")
            self.core.records.put("card", channel, {"ts": ts})
            return {"text": "スタンプを押したよ", "channel": await self.core.channel_name(channel)}
        if name == "press_stamp":
            self.pressed.append(name)
            card = self.core.records.get("card", channel)
            await self.core.update(channel, card["ts"], "押された")
            return {"pressed": True}
        return None

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
    monkeypatch.setattr(runner, "run_model", FakeAI())
    assistant, slack = make_assistant(config, store, {"C1": "1-vlm"})
    return assistant, slack


async def test_a_module_answers_mcp_operations(env):
    assistant, slack = env
    reply = await assistant.module_head_action("stamp", {"channel": "C1"})
    assert reply == {"text": "スタンプを押したよ", "channel": "1-vlm"}
    assert slack.posted()[-1]["text"] == "スタンプ"
    assert await assistant.module_head_action("press_stamp", {"channel": "C1"}) == {"pressed": True}
    assert assistant.modules["stamp"].pressed == ["press_stamp"]
    assert any(name == "chat_update" and kw["text"] == "押された" for name, kw in slack.calls)


async def test_unknown_mcp_operations_are_refused(env):
    assistant, slack = env
    with pytest.raises(ValueError, match="受け持つモジュールがありません"):
        await assistant.module_head_action("unknown_stamp", {})
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


@pytest.mark.parametrize("declaration", [
    '[slash_commands]\nstamp = "スタンプを押す"\n',
    '[slash_commands]\n',
    'slash_commands = "stamp"\n',
])
def test_legacy_slash_declarations_explain_mcp_migration(tmp_path, declaration):
    folder = _stamp(tmp_path, 'api = 2\nname = "stamp"\n' + declaration)
    with pytest.raises(modules.ModuleError, match=r"slash_commands.*MCP.*head_action.*移行"):
        modules.load_spec(folder)


@pytest.mark.parametrize("hook", ["home", "welcome", "on_home_action", "on_slash_command", "on_action", "on_view", "on_reaction"])
def test_legacy_ui_hooks_explain_mcp_migration(tmp_path, hook):
    code = f"class Module:\n    async def {hook}(self, *args):\n        pass\n"
    folder = _stamp(tmp_path, code=code)
    with pytest.raises(modules.ModuleError, match=rf"{hook}.*MCP.*head_action / head_materials.*移行"):
        modules.load_code(modules.load_spec(folder))


@pytest.mark.parametrize(("hook", "declaration"), [
    ("head_action", "def head_action(self, name, params):"),
    ("head_action", "async def head_action(self, name):"),
    ("head_action", "async def head_action(self, name, params, required):"),
    ("head_action", "async def head_action(self, *, name, params):"),
    ("head_materials", "def head_materials(self, days):"),
    ("head_materials", "async def head_materials(self):"),
    ("head_materials", "async def head_materials(self, days, required):"),
    ("head_materials", "async def head_materials(self, *, days):"),
])
def test_mcp_hooks_require_the_async_call_shape(tmp_path, hook, declaration):
    folder = _stamp(tmp_path, code=f"class Module:\n    {declaration}\n        return {{}}\n")
    with pytest.raises(modules.ModuleError, match=rf"{hook}.*async def {hook}"):
        modules.load_code(modules.load_spec(folder))


@pytest.mark.parametrize("hook", ["head_action", "head_materials"])
def test_mcp_hooks_must_be_functions(tmp_path, hook):
    folder = _stamp(tmp_path, code=f"class Module:\n    {hook} = 42\n")
    with pytest.raises(modules.ModuleError, match=rf"{hook}.*async def {hook}"):
        modules.load_code(modules.load_spec(folder))


def test_valid_mcp_hooks_load_without_ui_declarations(tmp_path):
    code = """class Module:
    async def head_action(self, name, params):
        return None

    async def head_materials(self, days):
        return {"stamps": []}
"""
    assert modules.load_code(modules.load_spec(_stamp(tmp_path, code=code))) is not None


def test_the_old_api_version_explains_mcp_migration(tmp_path):
    folder = _stamp(tmp_path, STAMP_TOML.replace("api = 2", "api = 1"))
    with pytest.raises(modules.ModuleError, match=r"枠の版.*api = 2.*MCP.*head_action / head_materials"):
        modules.load_spec(folder)
