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


def test_a2a_app_refuses_to_start_without_a_password():
    from kei_agent_course.app import build_app

    with pytest.raises(RuntimeError, match="KEI_AGENT_A2A_TOKEN"):
        build_app("http://127.0.0.1:8787", "")


def test_a2a_app_refuses_a_blank_password():
    from kei_agent_course.app import build_app

    with pytest.raises(RuntimeError, match="KEI_AGENT_A2A_TOKEN"):
        build_app("http://127.0.0.1:8787", "   ")


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


@pytest.fixture
async def stranger():
    """同じポートで待っている、A2A ではない誰か（JSON を返さない）。"""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    port = _free_port()

    async def card(request):
        # 名刺だけは読めるが、仕事の窓口は JSON を返さない
        return PlainTextResponse(json.dumps({"url": f"http://127.0.0.1:{port}/a2a"}),
                                 media_type="application/json")

    async def hello(request):
        return PlainTextResponse("<html>Not an agent</html>")

    app = Starlette(routes=[Route("/.well-known/agent-card.json", card),
                            Route("/{path:path}", hello, methods=["GET", "POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


async def test_a_reply_that_is_not_json_is_an_a2a_error(stranger):
    """別のものが同じポートにいても、ValueError を素で投げず A2AError にする。"""
    with pytest.raises(A2AError, match="SendMessage の返事が JSON ではありません"):
        await Agent(stranger, TOKEN).ask("list-due")
    with pytest.raises(A2AError, match="名刺が JSON ではありません"):
        await Agent(stranger + "/nope", TOKEN).card()


async def test_card_tells_what_the_agent_can_do(server):
    card = await Agent(server, TOKEN).card()
    assert card["name"].startswith("Kei Agent")
    assert [s["id"] for s in card["skills"]] == [
        "sync-assignments", "list-due", "list-calendar-assignments", "list-classes",
        "list-current-courses", "record-study-time", "ask", "time-report"]
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


async def test_calendar_assignments_pagination_failure_is_a_failed_task(server, monkeypatch):
    from kei_agent.notion import NotionError
    from kei_agent_course import notion_sync

    def broken_snapshot(days, today):
        raise NotionError("課題 DB の次ページを読めません")

    monkeypatch.setattr(notion_sync, "list_calendar_assignments", broken_snapshot)
    result = await Agent(server, TOKEN).ask("list-calendar-assignments", params={"days": 30})

    assert not result.ok
    assert "次ページ" in json.loads(result.answer)["text"]
    assert "NOTION_COURSE_TOKEN" not in result.answer


async def test_ask_gets_the_question_not_the_envelope(server, monkeypatch):
    """本体は session_id などを添えた JSON で頼む。claude に渡すのは質問だけにする。"""
    from kei_agent_a2a import claude

    seen = {}

    async def ask_connector(config, prompt, allowed, plugin_dir, deny=(), timeout_minutes=3, **_kwargs):
        seen["prompt"] = prompt
        return "過去問は Box にあるよ"

    monkeypatch.setattr(claude, "ask_connector", ask_connector)
    payload = json.dumps({"prompt": "情報Bの過去問ある？", "session_id": None,
                          "channel": "C123", "thread_ts": "1.2"}, ensure_ascii=False)

    result = await Agent(server, TOKEN).stream("ask", text=payload)

    assert result.ok and json.loads(result.answer)["text"] == "過去問は Box にあるよ"
    assert seen["prompt"].endswith("情報Bの過去問ある？")
    # スレッドの鍵やチャンネル ID を、質問として claude に渡さない
    assert "C123" not in seen["prompt"] and "session_id" not in seen["prompt"]


async def test_unknown_skill_fails_with_a_reason(server):
    result = await Agent(server, TOKEN).ask("", text="よろしく")
    assert not result.ok and "どの仕事か分かりません" in json.loads(result.answer)["text"]


def test_course_allows_every_notion_operation_but_no_box_write():
    """授業ホームの中では Notion を自由に使える。Box は読むだけ。"""
    from kei_agent_course import tools

    assert "mcp__claude_ai_Notion__notion-create-database" in tools.ALLOWED
    assert "mcp__claude_ai_Notion__notion-move-pages" in tools.ALLOWED
    assert "mcp__claude_ai_Notion__notion-duplicate-page" in tools.ALLOWED
    assert not any(name.endswith(("upload_file", "upload_file_version", "create_folder", "move_file",
                                  "move_folder", "copy_file", "create_file_comment")) for name in tools.ALLOWED)


def test_course_still_denies_box_writes_by_name():
    from kei_agent_course import tools

    assert "mcp__claude_ai_Box__upload_file" in tools.DENY
    assert not set(tools.ALLOWED) & set(tools.DENY)
