"""声のモジュール（modules/voice/）。本体側の差し込み口（出来事・App Home）と、載せ替えのときの移し替え。

担当プロセス側（喋る・聞く・道具）は test_voice.py。
"""

import asyncio
import json

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import modules, runner
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager
from kei_agent.store import Store


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    assistant = Assistant(config, store, FakeSlack({"C1": "vlm"}), JobManager(config, store, FakePueue()),
                          "xoxb-test", "UBOT")
    told = []

    async def tell_agent(skill, payload):
        told.append((skill, payload))
        return True

    monkeypatch.setattr(assistant.cores["voice"], "tell_agent", tell_agent)
    return assistant, told


async def settle(assistant):
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks), return_exceptions=True)
        await asyncio.sleep(0)


def test_voice_is_a_module_with_its_own_process():
    """声は modules/voice/ のモジュール。担当プロセスは共通のコマンド（kei-agent-module voice）で動く。"""
    spec = modules.builtin()["voice"]
    assert (spec.label, spec.port, spec.actor, spec.channels) == ("声", 8790, None, {})
    code = modules.load_agent(spec)
    assert [skill.id for skill in code.SKILLS] == ["notify"] and callable(code.background)


async def test_events_are_passed_on_only_while_notices_are_on(env):
    """出来事は、App Home の「知らせる」が入っているときだけ、声の担当に渡す（既定は切）。"""
    assistant, told = env
    assistant.emit("done", theme="vlm")
    await settle(assistant)
    assert told == []

    assistant.cores["voice"].records.put("switch", "notify", {"on": True})
    assistant.emit("done", theme="vlm")
    assistant.cores["course"].emit("due", title="レポート", at="2026-09-28T23:59")
    await settle(assistant)
    assert told == [("notify", {"kind": "done", "theme": "vlm"}),
                    ("notify", {"kind": "due", "title": "レポート", "at": "2026-09-28T23:59"})]


async def test_the_microphone_can_be_switched_even_while_notices_are_off(env):
    assistant, told = env
    await assistant.module_home_action("voice", "switches", {"selected_options": [{"value": "listen"}]})
    assert told == [("notify", {"kind": "listen", "on": True})]
    assert not assistant.modules["voice"].is_on("notify")


def test_the_old_voice_switches_move_to_the_voice_module(tmp_path):
    """本体が持っていた App Home の声のチェック（voice.enabled / voice.listening）は、声のモジュールの記録に移す。"""
    path = tmp_path / "state.db"
    store = Store(path)
    store.set_setting("voice.enabled", "1")
    store.set_setting("voice.listening", "0")

    store = Store(path)

    assert json.loads(store.module_record("voice", "switch", "notify")["value"]) == {"on": True}
    assert json.loads(store.module_record("voice", "switch", "listen")["value"]) == {"on": False}
    assert store.conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'voice.%'").fetchone()[0] == 0
    Store(path)     # 2回目は何もしない
