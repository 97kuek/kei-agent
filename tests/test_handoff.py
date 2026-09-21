"""長くなったスレッドを区切って、新しいスレッドで続ける流れ（handoff.py）。"""

import asyncio
from dataclasses import replace

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import runner
from kei_agent.assistant import Assistant
from kei_agent.handoff import ACCEPT_ACTION, DECLINE_ACTION, handoff_title, split_memo, strip_handoff
from kei_agent.jobs import JobManager


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C9": "00_kei-agent"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_claude", claude)
    assistant = Assistant(replace(config, handoff_after_turns=3), store, slack,
                          JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    return assistant, slack, claude


async def settle(assistant):
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks))


def buttons(slack):
    return [el for kw in slack.posted() for b in kw.get("blocks") or [] if b.get("type") == "actions"
            for el in b["elements"] if el["action_id"] in (ACCEPT_ACTION, DECLINE_ACTION)]


def press(el, user="UME"):
    return {"user": {"id": user}, "actions": [{"action_id": el["action_id"], "value": el["value"]}],
            "container": {"channel_id": "C1", "message_ts": "99.1"}}


async def ask(assistant, ts, text="続けて", thread_ts="10.1"):
    if ts == thread_ts:
        await assistant.on_mention({"channel": "C1", "user": "UME", "ts": ts, "text": f"<@UBOT> {text}"})
    else:
        await assistant.on_message({"channel": "C1", "user": "UME", "ts": ts, "thread_ts": thread_ts, "text": text})
    await settle(assistant)


def test_marker_gives_the_title_and_is_hidden_from_the_reply():
    text = "データの準備が終わったよ。\n\n🧵 区切り: ベースラインの検討"
    assert handoff_title(text) == "ベースラインの検討"
    assert handoff_title("ふつうの返事") is None
    assert strip_handoff(text) == "データの準備が終わったよ。"


def test_memo_is_split_into_title_and_body():
    assert split_memo("**ベースラインの検討**\n**目的**\n- 比べる") == ("ベースラインの検討", "**目的**\n- 比べる")
    assert split_memo("") == ("続き", "")


async def test_button_appears_when_claude_marks_a_break(env):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "準備が終わったよ。\n🧵 区切り: ベースラインの検討"}]
    await ask(assistant, "10.1")

    accept, decline = buttons(slack)
    assert accept["value"] == "10.1"
    offer = next(kw for kw in slack.posted() if kw.get("blocks") and "区切" in kw["text"])
    assert "ベースラインの検討" in offer["text"]
    assert slack.streamed() == ["準備が終わったよ。"]  # 合図の行は本文に出さない


async def test_button_appears_after_many_turns_and_not_again_right_after_declining(env):
    assistant, slack, claude = env
    for i, ts in enumerate(("10.1", "10.2", "10.3")):
        await ask(assistant, ts)
        assert len(buttons(slack)) == (2 if i == 2 else 0)

    await assistant.on_handoff_action(press(buttons(slack)[1]))  # このまま続ける
    await ask(assistant, "10.4")
    await ask(assistant, "10.5")
    assert len(buttons(slack)) == 2  # 断ってから3回たつまでは出さない
    await ask(assistant, "10.6")
    assert len(buttons(slack)) == 4


async def test_no_button_while_waiting_for_an_answer(env):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "どっちにする？\n❓ 確認: A と B のどちらで進める？\n🧵 区切り: 次"}]
    await ask(assistant, "10.1")
    assert buttons(slack) == []


async def test_no_button_in_the_improve_channel(env, config):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "案だよ\n🧵 区切り: 次"}]
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 直して"})
    await settle(assistant)
    assert buttons(slack) == []


async def test_accepting_posts_a_new_thread_that_starts_from_the_memo(env, store, config):
    assistant, slack, claude = env
    claude.behaviors = [
        {"text": "準備が終わったよ。\n🧵 区切り: ベースライン"},
        {"text": "ベースラインの検討\n**目的**\n- 窓の選び方を比べる\n**次にやること**\n- 小さく試す",
         "session_id": "sess-1"},
        {"text": "試したよ", "session_id": "sess-2"},
    ]
    await ask(assistant, "10.1")
    await assistant.on_handoff_action(press(buttons(slack)[0]))
    await settle(assistant)

    memo_call = claude.calls[1]
    assert memo_call["session_id"] == "sess-1" and "引き継ぎメモ" in memo_call["prompt"]
    top = next(kw for kw in slack.posted() if "thread_ts" not in kw)
    assert top["markdown_text"].startswith("🧵 **ベースラインの検討**") and "前のスレッド" in top["markdown_text"]
    new_ts = store.get_thread("C1", "10.1")["handed_off_to"]
    assert new_ts
    assert any(kw.get("thread_ts") == "10.1" and "新しいスレッド" in (kw.get("text") or "") for kw in slack.posted())
    update = [kw for name, kw in slack.calls if name == "chat_update"][-1]
    assert "引き継いで" in update["text"]

    # 新しいスレッドへの返信は、前の会話を持ち越さず、引き継ぎメモから始める
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "30.1", "thread_ts": new_ts, "text": "試して"})
    await settle(assistant)
    first = claude.calls[2]
    assert first["session_id"] is None and "<handoff>" in first["prompt"] and first["prompt"].endswith("試して")
    assert ".kei-agent/threads/10.1.md" in first["prompt"]
    # 2回目からは、ふつうに会話を続ける
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "30.2", "thread_ts": new_ts, "text": "次"})
    await settle(assistant)
    assert claude.calls[3]["session_id"] == "sess-2" and claude.calls[3]["prompt"] == "次"
    log = (config.research_root / "vlm" / ".kei-agent" / "threads" / f"{new_ts}.md").read_text()
    assert "引き継ぎ" in log and "窓の選び方" in log


async def test_pressing_twice_hands_off_once(env, store):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "区切るね\n🧵 区切り: 次"}, {"text": "次\n**目的**\n- x"}]
    await ask(assistant, "10.1")
    accept = buttons(slack)[0]
    await assistant.on_handoff_action(press(accept))
    await assistant.on_handoff_action(press(accept))
    await settle(assistant)
    assert len([kw for kw in slack.posted() if "thread_ts" not in kw]) == 1
    assert len(claude.calls) == 2


async def test_only_the_owner_can_hand_off(env, store):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "区切るね\n🧵 区切り: 次"}]
    await ask(assistant, "10.1")
    await assistant.on_handoff_action(press(buttons(slack)[0], user="USOMEONE"))
    await settle(assistant)
    assert store.get_thread("C1", "10.1")["handed_off_to"] is None and len(claude.calls) == 1


async def test_failed_memo_keeps_the_thread(env, store):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "区切るね\n🧵 区切り: 次"}, {"text": "", "is_error": True, "errors": ["boom"]}]
    await ask(assistant, "10.1")
    await assistant.on_handoff_action(press(buttons(slack)[0]))
    await settle(assistant)
    assert store.get_thread("C1", "10.1")["handed_off_to"] is None
    assert "作れなかった" in slack.texts()[-1]
