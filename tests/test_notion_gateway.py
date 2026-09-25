from __future__ import annotations

import pytest
from fakes import check_notion_body


class FakeNotion:
    def __init__(self, parents=None):
        self.parents = parents or {}
        self.calls = []

    def request(self, method, path, body=None):
        check_notion_body(body)
        self.calls.append((method, path, body))
        item_id = path.rsplit("/", 1)[-1]
        if item_id not in self.parents:
            from kei_agent.notion import NotionError
            raise NotionError(f"not found: {item_id}")
        parent = self.parents[item_id]
        return {"id": item_id, "parent": {"type": "page_id", "page_id": parent}}


def test_gateway_requires_both_tokens(config):
    from kei_agent_notion_gateway.config import load_gateway_config

    with pytest.raises(RuntimeError, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        load_gateway_config(config, {"NOTION_TOKEN": "notion"})
    with pytest.raises(RuntimeError, match="NOTION_TOKEN"):
        load_gateway_config(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "gateway"})


def test_gateway_is_loopback_and_uses_research_notion_state(config):
    from kei_agent_notion_gateway.config import load_gateway_config

    result = load_gateway_config(config, {
        "KEI_AGENT_NOTION_GATEWAY_TOKEN": "gateway",
        "NOTION_TOKEN": "notion",
    })

    assert result.host == "127.0.0.1"
    assert result.port == 8791
    assert result.state_path == config.state_dir / "notion.json"


def test_scope_accepts_root_and_descendants():
    from kei_agent_notion_gateway.scope import ResearchScope

    scope = ResearchScope(FakeNotion({"child": "root", "grand": "child"}), "root")
    scope.require("root")
    scope.require("grand")


@pytest.mark.parametrize(("parents", "item"), [
    ({}, "outside"),
    ({"a": "b", "b": "a"}, "a"),
])
def test_scope_rejects_unknown_and_cycles(parents, item):
    from kei_agent_notion_gateway.scope import ResearchScope, ScopeError

    with pytest.raises(ScopeError):
        ResearchScope(FakeNotion(parents), "root").require(item)


def test_scope_rejects_excessive_depth():
    from kei_agent_notion_gateway.scope import ResearchScope, ScopeError

    with pytest.raises(ScopeError, match="深すぎ"):
        ResearchScope(FakeNotion({"a": "b", "b": "root"}), "root", max_depth=1).require("a")


# Task 3: 型付きの Notion 操作


class FakeScope:
    """どの ID を検証したかだけを覚える scope。"""

    root_id = "root"

    def __init__(self):
        self.required: list[str] = []
        self.forgotten = 0

    def require(self, item_id: str) -> None:
        self.required.append(item_id)

    def forget(self) -> None:
        self.forgotten += 1

    def fetch(self, item_id: str) -> dict:
        return {"id": item_id}


class RecordingNotion:
    """呼ばれた Notion API を覚えるだけの偽物。"""

    def __init__(self, response=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.response = response if response is not None else {"id": "x"}

    def request(self, method, path, body=None):
        check_notion_body(body)
        self.calls.append((method, path, body))
        return dict(self.response)

    def children(self, block_id):
        self.calls.append(("GET", f"/blocks/{block_id}/children", None))
        return []


@pytest.fixture
def scope():
    return FakeScope()


@pytest.fixture
def notion():
    return RecordingNotion()


@pytest.fixture
def service(notion, scope):
    import logging

    from kei_agent_notion_gateway.service import ResearchNotion

    return ResearchNotion(notion, scope, logging.getLogger("kei-agent-notion-gateway"))


@pytest.mark.parametrize(("operation", "args", "checked"), [
    ("read", ("target",), ["target"]),
    ("query", ("source",), ["source"]),
    ("create_page", ("parent", {"title": "x"}, []), ["parent"]),
    ("update_page", ("target", {"title": "x"}), ["target"]),
    ("append_blocks", ("target", []), ["target"]),
    ("archive", ("target",), ["target"]),
    ("move", ("source", "destination"), ["source", "destination"]),
    ("duplicate", ("source", "destination"), ["source", "destination"]),
    ("create_database", ("parent", "題", {}), ["parent"]),
    ("update_database", ("target", {}), ["target"]),
])
def test_every_operation_checks_the_research_home(service, scope, operation, args, checked):
    getattr(service, operation)(*args)

    assert scope.required == checked


def test_search_keeps_only_results_inside_the_research_home(notion, scope):
    import logging

    from kei_agent_notion_gateway.scope import ScopeError
    from kei_agent_notion_gateway.service import ResearchNotion

    notion.response = {"results": [{"id": "inside"}, {"id": "outside"}]}

    class OneOutside(FakeScope):
        def require(self, item_id):
            super().require(item_id)
            if item_id == "outside":
                raise ScopeError("研究ホーム外の対象です")

    only_inside = OneOutside()
    service = ResearchNotion(notion, only_inside, logging.getLogger("kei-agent-notion-gateway"))

    assert service.search("量子") == [{"id": "inside"}]


def test_each_operation_forgets_the_parent_cache_first(service, scope):
    service.read("target")
    service.read("target")

    assert scope.forgotten == 2


def test_audit_omits_body_and_tokens(service, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="kei-agent-notion-gateway"):
        service.create_page("parent", {"secret": "do-not-log"}, [{"paragraph": "do-not-log"}])

    assert "do-not-log" not in caplog.text
    assert "Bearer" not in caplog.text
    assert "create_page" in caplog.text and "parent" in caplog.text


def test_audit_records_the_error_type_without_the_message(service, notion, caplog):
    import logging

    from kei_agent.notion import NotionError

    def fail(method, path, body=None):
        raise NotionError("401 Unauthorized: Bearer ntn_do-not-log")

    notion.request = fail
    with caplog.at_level(logging.INFO, logger="kei-agent-notion-gateway"), pytest.raises(NotionError):
        service.archive("target")

    assert "do-not-log" not in caplog.text
    assert "NotionError" in caplog.text


def test_record_time_writes_research_log_once(service, notion):
    service._time_logs = "time-logs"
    service.record_time("entry-1", "2026-09-23T10:00:00+09:00", 25, "amr", "実装", "https://slack.example/p1")
    service.record_time("entry-1", "2026-09-23T10:00:00+09:00", 25, "amr", "実装", "https://slack.example/p1")

    posts = [call for call in notion.calls if call[:2] == ("POST", "/pages")]
    assert len(posts) == 1
    assert posts[0][2]["parent"] == {"type": "data_source_id", "data_source_id": "time-logs"}


# Task 4: MCP サーバーと Bearer 認証

TOOLS = {
    "read", "search", "query", "create_page", "update_page", "append_blocks",
    "archive", "move", "duplicate", "create_database", "update_database",
}


@pytest.fixture
def settings(config):
    from kei_agent_notion_gateway.config import load_gateway_config

    return load_gateway_config(config, {
        "KEI_AGENT_NOTION_GATEWAY_TOKEN": "gateway-token",
        "NOTION_TOKEN": "notion-token",
    })


@pytest.fixture
def app(settings, service):
    from kei_agent_notion_gateway.app import build_app

    return build_app(settings, service)


async def test_mcp_exposes_only_typed_research_tools(service):
    from kei_agent_notion_gateway.app import build_mcp

    assert {tool.name for tool in await build_mcp(service).list_tools()} == TOOLS


async def test_health_is_open_but_the_mcp_endpoint_needs_the_gateway_token(app):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/mcp", json={})).status_code == 401
        wrong = await client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401


async def test_time_logs_endpoint_needs_the_gateway_token(app):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        assert (await client.post("/time-logs", json={})).status_code == 401
        response = await client.post("/time-logs", json={}, headers={"Authorization": "Bearer gateway-token"})
        assert response.status_code == 400


async def test_mcp_round_trip_creates_a_page_and_records_only_the_target(service, notion, caplog):
    import logging

    from mcp import Client

    from kei_agent_notion_gateway.app import build_mcp

    with caplog.at_level(logging.INFO, logger="kei-agent-notion-gateway"):
        async with Client(build_mcp(service), raise_exceptions=True) as client:
            await client.call_tool("create_page", {
                "parent_id": "parent", "properties": {"title": "do-not-log"}, "children": []})

    method, path, body = notion.calls[-1]
    assert (method, path) == ("POST", "/pages")
    assert body["parent"] == {"type": "page_id", "page_id": "parent"}
    assert "do-not-log" not in caplog.text


async def test_mcp_refuses_a_target_outside_the_research_home(notion):
    import logging

    from mcp import Client

    from kei_agent_notion_gateway.app import build_mcp
    from kei_agent_notion_gateway.scope import ResearchScope
    from kei_agent_notion_gateway.service import ResearchNotion

    outside = ResearchNotion(FakeNotion({}), ResearchScope(FakeNotion({}), "root"),
                             logging.getLogger("kei-agent-notion-gateway"))
    async with Client(build_mcp(outside)) as client:
        result = await client.call_tool("archive", {"target_id": "somewhere-else"})

    assert result.is_error
    assert "研究ホーム外" in str(result.content)


async def test_gateway_serves_a_real_mcp_session_over_http(settings, service):
    """本物の HTTP で立てて、合言葉つきの MCP が最後まで通ることを見る。"""
    import asyncio

    import httpx2
    import uvicorn
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from kei_agent_notion_gateway.app import build_app

    class Streams:
        def __init__(self, reader, writer):
            self.pair = (reader, writer)

        async def __aenter__(self):
            return self.pair

        async def __aexit__(self, *exc):
            return False

    server = uvicorn.Server(uvicorn.Config(build_app(settings, service), host="127.0.0.1", port=0,
                                           log_level="warning"))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(200):  # uvicorn が待ち受けるまで
            if server.started:
                break
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        headers = {"Authorization": f"Bearer {settings.token}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (r, w),
            Client(Streams(r, w)) as client,
        ):
            assert {tool.name for tool in (await client.list_tools()).tools} == TOOLS
            result = await client.call_tool("create_page", {"parent_id": "parent", "properties": {}})

        assert not result.is_error
    finally:
        server.should_exit = True
        await serving
