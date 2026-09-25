"""流しながら受け取る口（stream）が、つながらない・壊れた行を呼ぶ側の失敗に揃えること。"""

import socket

import pytest
from aiohttp import web

from kei_agent import a2a


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_stream_to_a_closed_port_is_not_reachable():
    """入れ直しの最中（ポートが閉じている）は、agents.ask がやり直せる NotReachable にする。"""
    agent = a2a.Agent(f"http://127.0.0.1:{_free_port()}", timeout=5)
    agent._rpc_url = agent.base_url + "/a2a"

    with pytest.raises(a2a.NotReachable):
        await agent.stream("ask", "こんにちは")


@pytest.fixture
async def huge_line_server():
    """上限を超える長さの1行を、改行なしで流してくる相手。"""
    async def rpc(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b"data: " + b"x" * (1024 * 1024))
        await resp.write(b"\n")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/a2a", rpc)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def test_stream_with_a_too_long_line_is_an_a2a_error(huge_line_server):
    agent = a2a.Agent(huge_line_server, timeout=5)
    agent._rpc_url = huge_line_server + "/a2a"

    with pytest.raises(a2a.A2AError):
        await agent.stream("ask", "こんにちは")
