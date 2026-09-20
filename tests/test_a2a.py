"""オーケストレーター（Kei Agent 本体）と、大学エージェント（A2A サーバー）の往復。

本物のサーバーを 127.0.0.1 に立てて、名刺を読み、仕事を頼んで、結果が返るところまでを見る。
"""

import asyncio
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
    assert [s["id"] for s in card["skills"]] == ["sync-assignments", "list-due", "time-report"]
    # 流しながら返す機能は持たない、と正直に書く
    assert card["capabilities"].get("streaming") in (False, None)
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


async def test_card_is_readable_without_the_password(server):
    assert (await Agent(server).card())["name"].startswith("Kei Agent")


async def test_work_needs_the_password(server):
    with pytest.raises(A2AError):
        await Agent(server, "違う合言葉").ask("list-due")


async def test_asking_a_skill_comes_back_with_an_answer(server):
    result = await Agent(server, TOKEN).ask("sync-assignments")
    assert result.ok and result.task_id
    # 中身はまだないので、何が足りないかを返す
    assert "MOODLE_TOKEN" in result.text


async def test_unknown_skill_fails_with_a_reason(server):
    result = await Agent(server, TOKEN).ask("", text="よろしく")
    assert not result.ok and "どの仕事か分かりません" in result.text
