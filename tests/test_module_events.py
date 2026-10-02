"""モジュールの枠の広がり（段階3の声の①）。出来事の受け口、App Home の項目、担当プロセス側の窓口。

声の担当を載せ替える前に、利用者のモジュールで確かめる。
"""

import asyncio
import json
from dataclasses import replace

import pytest
from fakes import FakeAI, home_action, make_assistant

from kei_agent.execution import runner
from kei_agent.framework import modules
from kei_agent.testing.kit import settle

LAMP_TOML = 'api = 1\nname = "lamp"\nlabel = "ランプ"\n'

LAMP_CODE = '''from kei_agent.api import Core, selected_values


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.events = []

    async def on_event(self, kind, data):
        self.events.append((kind, data))
        if kind == "boom":
            raise RuntimeError("壊れた")

    def home(self):
        on = (self.core.records.get("switch", "light") or {}).get("on", False)
        return [{"type": "actions", "elements": [
            self.core.home_checkboxes("light", {"on": "点ける"}, {"on"} if on else set())]}]

    async def on_home_action(self, name, action):
        self.core.records.put("switch", name, {"on": "on" in selected_values(action)})
'''


def _lamp(root, code=LAMP_CODE):
    folder = root / "lamp"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text(LAMP_TOML, encoding="utf-8")
    (folder / "module.py").write_text(code, encoding="utf-8")
    return folder


@pytest.fixture
def env(config, store, tmp_path, monkeypatch):
    _lamp(tmp_path / "user-modules")
    modules.register_user_modules(tmp_path / "user-modules")
    config = replace(config, modules=(*config.modules, "lamp"))
    monkeypatch.setattr(runner, "run_model", FakeAI())
    assistant, slack = make_assistant(config, store, {"C1": "vlm"})
    return assistant, slack




# 出来事（core.emit → on_event）

async def test_events_reach_every_module_that_listens(env, caplog):
    """本体やモジュールが配った出来事は、on_event を持つモジュールに届く（空の中身は外す）。受け手が壊れても送り手は止まらない。"""
    assistant, slack = env
    lamp = assistant.modules["lamp"]
    assistant.cores["course"].emit("due", title="レポート", at="2026-09-28T23:59", url="")
    assistant.emit("done", theme="vlm")
    assistant.emit("boom")
    await settle(assistant)
    assert lamp.events == [("due", {"title": "レポート", "at": "2026-09-28T23:59"}), ("done", {"theme": "vlm"}),
                           ("boom", {})]
    assert "出来事（boom）を受け取れませんでした" in caplog.text


# App Home（home → on_home_action）

def _published(slack):
    return [kw for name, kw in slack.calls if name == "views_publish"][-1]["view"]


async def test_a_module_puts_its_own_items_on_app_home_and_handles_them(env, store):
    """モジュールの項目は、表示名の見出しの下に並ぶ。押されたらそのモジュールが扱い、App Home を作り直す。"""
    assistant, slack = env
    await assistant.publish_home("UME")
    blocks = _published(slack)["blocks"]
    title = next(n for n, block in enumerate(blocks) if (block.get("text") or {}).get("text") == "*ランプ*")
    light, = blocks[title + 1]["elements"]
    assert light["action_id"] == "kei_agent_home_module:lamp:light" and "initial_options" not in light

    await assistant.on_home_action(home_action(light["action_id"], selected_options=[{"value": "on"}]))
    assert assistant.cores["lamp"].records.get("switch", "light") == {"on": True}
    light, = [e for b in _published(slack)["blocks"] for e in b.get("elements", [])
              if e.get("action_id") == "kei_agent_home_module:lamp:light"]
    assert [option["value"] for option in light["initial_options"]] == ["on"]


async def test_module_items_are_for_the_owner_and_a_broken_one_is_left_out(env, monkeypatch):
    """ほかの人には見せず、押されても扱わない。1つのモジュールの項目が作れなくても、App Home は出す。"""
    assistant, slack = env
    await assistant.on_home_action(home_action("kei_agent_home_module:lamp:light", user="USOMEONE",
                                           selected_options=[{"value": "on"}]))
    assert assistant.cores["lamp"].records.get("switch", "light") is None
    await assistant.publish_home("USOMEONE")
    assert "ランプ" not in json.dumps(_published(slack), ensure_ascii=False)

    monkeypatch.setattr(assistant.modules["lamp"], "home", lambda: 1 / 0)
    await assistant.publish_home("UME")
    shown = json.dumps(_published(slack), ensure_ascii=False)
    assert "ランプ" not in shown and "接続先" in shown


@pytest.mark.parametrize(("hook", "message"), [
    ("    async def on_event(self):\n        pass\n", "on_event(self, kind, data)"),
    ("    def home(self, user):\n        return []\n", "home(self)"),
    ("    async def on_home_action(self, name):\n        pass\n", "on_home_action(self, name, action)"),
])
def test_hooks_in_the_wrong_shape_are_refused_at_startup(tmp_path, hook, message):
    folder = _lamp(tmp_path, "class Module:\n    def __init__(self, core):\n        self.core = core\n\n" + hook)
    with pytest.raises(modules.ModuleError, match=message.replace("(", r"\(").replace(")", r"\)")):
        modules.load_code(modules.load_spec(folder))


# 担当プロセス側の窓口（background・records・ask_orchestrator・put_request）

BELL_CODE = '''import asyncio

from kei_agent_a2a.api import AgentSkill, SkillExecutor

SKILLS = [AgentSkill(id="ring", name="鳴らす", description="鳴らす", tags=["bell"])]
RAN = []


class Executor(SkillExecutor):
    async def handle(self, updater, metadata, text):
        await self.done(updater, "鳴らしたよ")


async def background(executor):
    RAN.append(("start", executor.records.get("switch", "sound")))
    try:
        await asyncio.Event().wait()
    finally:
        RAN.append(("stop", None))
'''


def _bell(root, code=BELL_CODE):
    folder = root / "bell"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text('api = 1\nname = "bell"\nlabel = "ベル"\n[process]\nport = 8802\n',
                                        encoding="utf-8")
    (folder / "agent.py").write_text(code, encoding="utf-8")
    return folder


async def test_a_module_process_keeps_its_background_work_running(tmp_path, config, store):
    """background は担当と同じプロセスで、起動のときに始まり、止めるときに止まる。本体と同じ記録を読める。"""
    pytest.importorskip("a2a", reason="担当プロセスは a2a-sdk で動く")
    from kei_agent.storage.records import Records
    from kei_agent_a2a import launch

    spec = modules.load_spec(_bell(tmp_path))
    code = modules.load_agent(spec)
    Records(store, "bell").put("switch", "sound", {"on": True})
    app = launch.build_app(spec, "http://127.0.0.1:8802", "token", executor=code.Executor(config, store))
    # uvicorn が起動と終了のときに通るところ（lifespan）を、そのまま通す
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert code.RAN == [("start", {"on": True})]
    assert code.RAN[-1] == ("stop", None)


@pytest.mark.parametrize("background", ["def background(executor):\n    pass\n",
                                        "async def background():\n    pass\n"])
def test_background_must_be_an_async_function_of_the_executor(tmp_path, background):
    code = BELL_CODE.split("async def background")[0] + background
    with pytest.raises(modules.ModuleError, match="async def background"):
        modules.load_agent(modules.load_spec(_bell(tmp_path, code)))


async def test_a_module_process_can_ask_the_orchestrator(config, monkeypatch):
    """研究・大学・仕事の中身は、本体の問い合わせ口に聞く（担当を呼べるのは本体だけ）。"""
    pytest.importorskip("a2a", reason="担当プロセスは a2a-sdk で動く")
    from kei_agent_a2a import api

    asked = []

    async def ask(agent, skill, text="", **_kwargs):
        asked.append((agent.base_url, skill, json.loads(text)))
        return api.agents.Reply.broken("断られた") if "失敗" in text else api.agents.Reply(True, "月曜は2コマ", {})

    monkeypatch.setattr(api.agents, "ask", ask)
    config = replace(config, a2a=replace(config.a2a, orchestrator="http://127.0.0.1:8786"))
    assert await api.ask_orchestrator(config, "course", "月曜の授業は？") == "月曜は2コマ"
    assert asked == [("http://127.0.0.1:8786", "ask", {"actor": "course", "question": "月曜の授業は？", "theme": ""})]
    with pytest.raises(api.OrchestratorError, match="断られた"):
        await api.ask_orchestrator(config, "course", "失敗して")
    with pytest.raises(api.OrchestratorError, match=r"\[a2a\] orchestrator"):
        await api.ask_orchestrator(replace(config, a2a=replace(config.a2a, orchestrator="")), "course", "x")


def test_a_module_process_can_leave_a_request_for_the_orchestrator(config):
    """Slack の外からの依頼は、本体が拾う置き場に置く（本体がテーマのチャンネルにスレッドを立てる）。"""
    pytest.importorskip("a2a", reason="担当プロセスは a2a-sdk で動く")
    from kei_agent.conversation import ask
    from kei_agent_a2a import api

    api.put_request(config, "vlm", "図を直して")
    api.put_request(config, "vlm", "締切は金曜に決めた", note=True)
    claimed = ask.claim_asks(config)
    # 同じミリ秒に置いたものは名前の順が決まらないので、順番は見ない
    assert {(item.payload["theme"], item.payload["text"], item.payload["kind"]) for item in claimed} == {
        ("vlm", "図を直して", "request"), ("vlm", "締切は金曜に決めた", "note")}


def test_the_process_side_records_are_the_modules_own(config, store):
    pytest.importorskip("a2a", reason="担当プロセスは a2a-sdk で動く")
    from kei_agent_a2a.api import SkillExecutor

    class Executor(SkillExecutor):
        agent = "bell"

    Executor(config, store).records.put("switch", "sound", {"on": False})
    assert store.module_record("bell", "switch", "sound") is not None
    assert store.module_record("lamp", "switch", "sound") is None
