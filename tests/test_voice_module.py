"""声のモジュール（modules/voice/）。本体側の差し込み口（出来事・MCP の通知設定）。

担当プロセス側（喋る・聞く・道具）は test_voice.py。
"""
import asyncio

import pytest
from fakes import FakeAI, make_assistant

from kei_agent.conversation.hands import Hands, HandsError
from kei_agent.execution import runner
from kei_agent.framework import modules
from kei_agent.testing.kit import settle


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeAI())
    assistant, _ = make_assistant(config, store, {"C1": "vlm"})
    told = []

    async def tell_agent(skill, payload):
        told.append((skill, payload))
        return True

    monkeypatch.setattr(assistant.cores["voice"], "tell_agent", tell_agent)
    return assistant, told




def test_voice_is_a_module_with_its_own_process():
    """声は modules/voice/ のモジュール。担当プロセスは共通のコマンド（kei-agent-module voice）で動く。"""
    spec = modules.builtin()["voice"]
    assert (spec.label, spec.port, spec.actor, spec.channels) == ("声", 8790, None, {})
    code = modules.load_agent(spec)
    assert [skill.id for skill in code.SKILLS] == ["notify"] and callable(code.background)


async def test_events_are_passed_on_only_while_notices_are_on(env):
    """出来事は、MCP の「知らせる」が入っているときだけ、声の担当に渡す（既定は切）。"""
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
    assert await Hands(assistant).voice(listen=True) == {"notify": False, "listen": True}
    assert told == [("notify", {"kind": "listen", "on": True})]
    assert not assistant.modules["voice"].is_on("notify")



async def test_mcp_switches_notices_and_the_microphone(env):
    """MCP の voice で切り替える。指定しなかったほうは変えない。"""
    assistant, told = env
    h = Hands(assistant)
    assert await h.voice(notify=True) == {"notify": True, "listen": False}
    assert told == []
    assert await h.voice(listen=True) == {"notify": True, "listen": True}
    assert told == [("notify", {"kind": "listen", "on": True})]
    assert await h.voice() == {"notify": True, "listen": True}


@pytest.mark.parametrize("key", ["notify", "listen"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, [], {}])
async def test_mcp_rejects_non_boolean_switches_without_changing_settings(env, key, value):
    """文字列などを真偽値へ変換して、意図せず音声を有効にしない。"""
    assistant, told = env
    with pytest.raises(HandsError, match=key):
        await Hands(assistant).voice(**{key: value})
    assert await Hands(assistant).voice() == {"notify": False, "listen": False}
    assert told == []


@pytest.mark.parametrize("listening", [True, False])
@pytest.mark.parametrize("raises", [True, False])
async def test_failed_microphone_delivery_keeps_both_previous_settings(env, monkeypatch, listening, raises):
    """担当が設定変更を受け取れなければ、保存済みの設定を維持してやり直せる。"""
    assistant, _ = env
    h = Hands(assistant)
    await h.voice(listen=listening)

    async def unavailable(skill, payload):
        if raises:
            raise ConnectionError("担当につながりません")
        return False

    monkeypatch.setattr(assistant.cores["voice"], "tell_agent", unavailable)
    with pytest.raises(HandsError, match="マイク"):
        await h.voice(notify=True, listen=not listening)
    assert await h.voice() == {"notify": False, "listen": listening}


async def test_mcp_closes_microphone_without_disabling_notices(env):
    assistant, told = env
    h = Hands(assistant)
    await h.voice(notify=True, listen=True)
    assert await h.voice(listen=False) == {"notify": True, "listen": False}
    assert told == [("notify", {"kind": "listen", "on": True}),
                    ("notify", {"kind": "listen", "on": False})]


async def test_mcp_serializes_microphone_changes_to_keep_latest_request(env, monkeypatch):
    """開く要求が担当へ届く途中でも、後から届いた閉じる要求を失わない。"""
    assistant, _ = env
    opened, received = asyncio.Event(), asyncio.Event()
    listening = False

    async def microphone(skill, payload):
        nonlocal listening
        listening = payload["on"]
        if listening:
            opened.set()
            await received.wait()
        return True

    monkeypatch.setattr(assistant.cores["voice"], "tell_agent", microphone)
    h = Hands(assistant)
    opening = asyncio.create_task(h.voice(listen=True))
    await opened.wait()
    closing = asyncio.create_task(h.voice(listen=False))
    await asyncio.sleep(0)
    received.set()
    await asyncio.gather(opening, closing)
    assert await h.voice() == {"notify": False, "listen": False}
    assert listening is False
