"""MCP の待機・実行・終了は AI の返答内容に依存せず判定する。"""

import asyncio

import pytest
from fakes import FakeAI, final_answer, make_assistant

from kei_agent.conversation import hands as hands_module
from kei_agent.conversation.hands import Hands
from kei_agent.execution import runner


@pytest.mark.parametrize("conversation", ["same", "other"])
async def test_mcp_status_tracks_queued_and_running_work_and_stops_elapsed_time(config, store, monkeypatch, conversation):
    assistant, _ = make_assistant(config, store)
    assistant.semaphore = asyncio.Semaphore(1)
    (config.research_root / "vlm").mkdir(parents=True)
    monkeypatch.setattr(hands_module, "SHORT_SECONDS", 0.01)
    ai = FakeAI()
    ai.answer(final_answer("一つ目が終わりました"))
    ai.answer(final_answer("二つ目が終わりました"))
    started, release = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        started.set()
        await release.wait()
        return await ai(*args, **kwargs)

    monkeypatch.setattr(runner, "run_model", run)
    h = Hands(assistant)
    first = await h.run("vlm", "一つ目", conversation="same")
    await started.wait()
    second = await h.run("vlm", "二つ目", conversation=conversation)
    try:
        assert h.status(first["ticket"])["phase"] == "running"
        assert h.status(second["ticket"])["phase"] == "queued"
        assert h.status(first["ticket"])["elapsed_seconds"] >= 0
    finally:
        release.set()
        await asyncio.gather(*assistant.hands_tasks.values())
    done = h.status(first["ticket"])
    assert done["status"] == done["phase"] == "done"
    finished = h.records.get("ticket", first["ticket"])["finished_at"]
    monkeypatch.setattr(hands_module.time, "time", lambda: finished + 3600)
    assert h.status(first["ticket"])["elapsed_seconds"] == done["elapsed_seconds"]


async def test_cancelled_mcp_work_is_persisted_as_failed(config, store, monkeypatch):
    assistant, _ = make_assistant(config, store)
    assistant.semaphore = asyncio.Semaphore(0)
    (config.research_root / "vlm").mkdir(parents=True)
    monkeypatch.setattr(hands_module, "SHORT_SECONDS", 0.01)
    h = Hands(assistant)
    accepted = await h.run("vlm", "順番待ちで中断", conversation="cancel")
    task = assistant.hands_tasks[accepted["ticket"]]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    result = h.status(accepted["ticket"])
    assert result["status"] == result["phase"] == "failed"
    assert "中断" in result["text"]
    assert h.records.get("ticket", accepted["ticket"])["finished_at"] > 0
