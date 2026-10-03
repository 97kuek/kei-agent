"""旧引き継ぎ記録の復旧と、背景処理の例外ログ。新しい引き継ぎは test_hands.py。"""

import asyncio
import logging

import pytest
from fakes import FakeAI, make_assistant

from kei_agent.conversation.request import Request
from kei_agent.execution import runner


@pytest.fixture
def env(config, store, monkeypatch):
    claude = FakeAI()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant, slack = make_assistant(config, store, {"C1": "vlm", "C9": "0-kei-agent"})
    return assistant, slack, claude


async def test_a_broken_background_job_is_logged(env, caplog):
    """裏で動かした仕事が落ちたら、必ずログに残す（黙って消えると原因が追えない）。"""
    assistant, *_ = env

    async def broken():
        raise RuntimeError("こわれた")

    with caplog.at_level(logging.ERROR, logger="kei_agent.conversation.assistant"):
        task = assistant.spawn(broken())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert any("裏で動かした仕事が落ちました" in r.message for r in caplog.records)


async def test_retired_handoff_is_closed_without_replaying_or_running_a_normal_request(env, store):
    """旧引き継ぎの中断は終了扱いにし、新 MCP からの再依頼を待つ。"""
    assistant, slack, claude = env
    req = Request("C1", "vlm", "10.1", None, "引き継ぎメモを書いて", trigger="handoff")
    store.upsert_thread("C1", "10.1", "vlm", "s1")
    run_id = store.start_run("C1", "10.1", "vlm", "handoff")
    store.start_in_flight(req.to_payload())

    assert await assistant.resume_interrupted() == 0
    await asyncio.gather(*list(assistant.tasks))
    assert claude.calls == []
    assert store.interrupted_requests() == []
    row = store.conn.execute("SELECT ended_at, is_error FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["ended_at"] is not None and row["is_error"] == 1
    assert any("もう一度頼んで" in text for text in slack.texts())
    notices = list(slack.texts())
    assert await assistant.resume_interrupted() == 0
    assert slack.texts() == notices


async def test_saved_local_handoff_memo_is_preserved_after_the_old_ui_is_removed(env, store):
    """既存の会話メモは、新しい実行セッションにも渡す。"""
    assistant, _, claude = env
    store.upsert_thread("C1", "10.2", "vlm", None)
    store.update_thread("C1", "10.2", handoff_memo="前の会話のメモ: 条件Bを比べる\n\n")
    await assistant.process(Request("C1", "vlm", "10.2", "10.2", "試して"))
    assert "条件Bを比べる" in claude.calls[-1]["prompt"]
    assert claude.calls[-1]["prompt"].endswith("試して")
