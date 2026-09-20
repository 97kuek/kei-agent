"""オーケストレーター（Kei Agent 本体）と、大学エージェント（A2A サーバー）の往復。

本物のサーバーを 127.0.0.1 に立てて、名刺を読み、仕事を頼んで、結果が返るところまでを見る。
"""

import asyncio
import json
import socket

import pytest

from kei_agent.a2a import A2AError, Agent

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")
pytest.importorskip("uvicorn")

TOKEN = "test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server():
    """大学エージェントを立てて、住所を返す。"""
    import uvicorn

    from kei_agent_course.app import build_app

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(build_app(base, TOKEN), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):  # 立ち上がるまで待つ
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield base
    server.should_exit = True
    await task


async def test_card_tells_what_the_agent_can_do(server):
    card = await Agent(server, TOKEN).card()
    assert card["name"].startswith("Kei Agent")
    assert [s["id"] for s in card["skills"]] == [
        "sync-assignments", "list-due", "list-classes", "ask", "time-report"]
    # 自由な質問（ask）は claude を動かすので、流しながら返す
    assert card["capabilities"].get("streaming") is True
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


async def test_card_is_readable_without_the_password(server):
    assert (await Agent(server).card())["name"].startswith("Kei Agent")


async def test_work_needs_the_password(server):
    with pytest.raises(A2AError):
        await Agent(server, "違う合言葉").ask("list-due")


async def test_asking_a_skill_comes_back_with_an_answer(server, monkeypatch):
    """締切の一覧が JSON で返る（見せ方はオーケストレーターが決める）。"""
    from datetime import datetime

    from kei_agent_course import ics, moodle

    monkeypatch.setenv("MOODLE_ICS_URL", "https://example.invalid/calendar.ics")
    monkeypatch.setattr(moodle, "due", lambda url, since=None, days=90: [
        ics.Event(uid="1@moodle", summary="第3回レポート の 提出期限",
                  starts_at=datetime(2026, 9, 25, 23, 59), course="データベース(2019ZZ)")])

    result = await Agent(server, TOKEN).ask("list-due", params={"days": 30})
    assert result.ok and result.task_id
    # 返事は全エージェント共通の封筒。中身は data に入る
    envelope = json.loads(result.answer)
    assert envelope["ok"] is True and "1 件" in envelope["text"]
    payload = envelope["data"]
    assert payload["days"] == 30 and payload["more"] == 0
    item, = payload["items"]
    assert item["id"] == "1@moodle" and item["course"] == "データベース"
    assert item["at"].startswith("2026-09-25T23:59")


async def test_sync_says_what_is_missing_without_the_calendar_url(server, monkeypatch):
    monkeypatch.delenv("MOODLE_ICS_URL", raising=False)
    result = await Agent(server, TOKEN).ask("sync-assignments")
    assert not result.ok and "カレンダーをエクスポート" in json.loads(result.answer)["text"]


async def test_list_due_says_what_is_missing_without_the_calendar_url(server, monkeypatch):
    monkeypatch.delenv("MOODLE_ICS_URL", raising=False)
    result = await Agent(server, TOKEN).ask("list-due")
    assert not result.ok and "カレンダーをエクスポート" in json.loads(result.answer)["text"]


async def test_unknown_skill_fails_with_a_reason(server):
    result = await Agent(server, TOKEN).ask("", text="よろしく")
    assert not result.ok and "どの仕事か分かりません" in json.loads(result.answer)["text"]
