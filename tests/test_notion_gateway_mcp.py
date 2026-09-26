"""Notion ゲートウェイの MCP の道具。名前と形はどの client でも同じで、届くのは client のホームの中だけ。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fakes import FakeNotionAPI
from mcp import Client
from test_notion_gateway import MASTER, area

from kei_agent.config import NotionConfig, notion_id
from kei_agent.notion import gateway_client_token
from kei_agent_notion_gateway import service
from kei_agent_notion_gateway.app import build_app, build_mcp
from kei_agent_notion_gateway.clients import client_roots
from kei_agent_notion_gateway.config import load_gateway_config
from kei_agent_notion_gateway.gateway import Gateway
from kei_agent_notion_gateway.service import NotionTools

TOOLS = {"read", "search", "query", "create_page", "update_page", "append_blocks", "replace_content",
         "update_block", "delete_block", "create_database", "update_data_source", "move"}


@pytest.fixture
def api():
    return FakeNotionAPI()


@pytest.fixture
def world(api):
    roots = {name: api.add_page(None, title) for name, title in
             (("hub", "Keitaro Ueki"), ("research", "研究ホーム"), ("course", "授業ホーム"), ("private", "日記"))}
    return SimpleNamespace(**{name: area(api, root) for name, root in roots.items()})


@pytest.fixture
def gw_config(config, world):
    return replace(config, notion=NotionConfig(hub_home=notion_id(world.hub.root),
                                               research_home=notion_id(world.research.root),
                                               course_home=notion_id(world.course.root)))


@pytest.fixture
def tools(api, gw_config):
    return NotionTools(Gateway(api, client_roots(gw_config.notion), logging.getLogger("kei-agent-notion-gateway")))


async def call(tools, name: str, client: str = "research", **arguments):
    async with Client(build_mcp(tools, default_client=client)) as session:
        return await session.call_tool(name, arguments)


async def ok(tools, name: str, client: str = "research", **arguments) -> dict:
    result = await call(tools, name, client, **arguments)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def last(api, method: str, path_prefix: str) -> dict:
    return next(body for m, path, body in reversed(api.forwarded) if m == method and path.startswith(path_prefix))


async def test_every_client_sees_the_same_twelve_tools(tools):
    for client in ("kei-agent", "research", "course"):
        assert {tool.name for tool in await build_mcp(tools, default_client=client).list_tools()} == TOOLS


# 読む


async def test_read_a_page_gives_properties_and_markdown_in_chunks(tools, api, world, monkeypatch):
    monkeypatch.setattr(service, "TEXT_CHARS", 10)
    api.markdown[api.key(world.research.row)] = "# 実験\n" + "あ" * 15
    first = await ok(tools, "read", target_id=world.research.row)
    assert first["object"] == "page" and first["title"] == "行"
    assert first["properties"]["名前"] == {"type": "title", "value": "行"}
    assert first["content"] == "# 実験\n" + "あ" * 5 and first["next_cursor"] == "10"
    rest = await ok(tools, "read", target_id=world.research.row, cursor=first["next_cursor"])
    assert rest["content"] == "あ" * 10 and "next_cursor" not in rest


async def test_read_databases_sources_views_and_blocks(tools, world):
    home = world.research
    database = await ok(tools, "read", target_id=home.db)
    assert database["object"] == "database" and database["data_sources"] == [
        {"id": notion_id(home.ds), "name": "Task"}]
    source = await ok(tools, "read", target_id=home.ds)
    assert source["schema"]["名前"] == {"type": "title"}
    assert (await ok(tools, "read", target_id=home.view))["name"] == "表"
    blocks = await ok(tools, "read", target_id=home.row, blocks=True)
    assert blocks["blocks"] == [{"id": notion_id(home.row_block), "type": "paragraph", "text": "本文"}]


async def test_search_shows_only_the_callers_home(tools, world):
    found = await ok(tools, "search", query="", filter="data_source")
    assert [item["id"] for item in found["results"]] == [notion_id(world.research.ds)]
    pages = await ok(tools, "search", "course", query="メモ")
    assert [item["id"] for item in pages["results"]] == [notion_id(world.course.page)]


async def test_query_takes_a_database_id_and_returns_plain_rows(tools, world):
    found = await ok(tools, "query", data_source_id=world.research.db,
                     filter={"property": "名前", "title": {"equals": "行"}})
    assert found["data_source_id"] == notion_id(world.research.ds)
    assert [row["properties"]["名前"]["value"] for row in found["results"]] == ["行"]


# 作る・直す


async def test_create_page_in_a_database_turns_plain_values_into_notion_properties(tools, api, world):
    db, ds = api.add_database(world.course.root, "課題", {
        "課題": {"title": {}}, "状態": {"status": {}}, "締切": {"date": {}}, "科目": {"relation": {}},
        "見積時間": {"number": {}}, "タグ": {"multi_select": {}}})
    created = await ok(tools, "create_page", "course", parent_id=db, title="第3回レポート", properties={
        "状態": "未着手", "締切": "2026-10-01", "科目": [world.course.page], "見積時間": 2, "タグ": ["重要"]})
    body = last(api, "POST", "/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": notion_id(ds)}
    assert body["properties"]["課題"]["title"][0]["text"]["content"] == "第3回レポート"
    assert body["properties"]["状態"] == {"status": {"name": "未着手"}}
    assert body["properties"]["締切"] == {"date": {"start": "2026-10-01"}}
    assert body["properties"]["科目"] == {"relation": [{"id": notion_id(world.course.page)}]}
    assert body["properties"]["見積時間"] == {"number": 2}
    assert body["properties"]["タグ"] == {"multi_select": [{"name": "重要"}]}
    assert created["title"] == "第3回レポート"


async def test_create_page_under_a_page_with_markdown_and_icon(tools, api, world):
    await ok(tools, "create_page", parent_id=world.research.root, title="考察", content="## 結果\n良かった", icon="📝")
    body = last(api, "POST", "/pages")
    assert body["parent"] == {"type": "page_id", "page_id": notion_id(world.research.root)}
    assert body["markdown"] == "## 結果\n良かった" and body["icon"] == {"type": "emoji", "emoji": "📝"}


async def test_create_page_says_which_column_is_missing(tools, world):
    result = await call(tools, "create_page", parent_id=world.research.ds, properties={"無い列": "x"})
    assert result.is_error and "無い列" in result.content[0].text


async def test_update_page_properties_icon_and_trash(tools, api, world):
    await ok(tools, "update_page", page_id=world.research.row, properties={"名前": "直した"}, icon="",
             in_trash=True)
    body = last(api, "PATCH", "/pages/")
    assert body["properties"]["名前"]["title"][0]["text"]["content"] == "直した"
    assert body["icon"] is None and body["in_trash"] is True


async def test_append_blocks_puts_markdown_after_a_block(tools, api, world):
    added = await ok(tools, "append_blocks", target_id=world.research.row, content="- 一つ目\n- 二つ目",
                     after=world.research.row_block)
    body = last(api, "PATCH", "/blocks/")
    assert [child["type"] for child in body["children"]] == ["bulleted_list_item"] * 2
    assert body["position"] == {"type": "after_block", "after_block": {"id": notion_id(world.research.row_block)}}
    assert added["added"] == 2


async def test_replace_content_keeps_child_pages_and_databases(tools, api, world):
    await ok(tools, "replace_content", page_id=world.research.page, content="# 新しい本文")
    body = last(api, "PATCH", "/pages/")
    assert body == {"type": "replace_content", "replace_content": {"new_str": "# 新しい本文"}}


async def test_update_block_text_and_delete_block(tools, api, world):
    await ok(tools, "update_block", block_id=world.research.row_block, text="直した本文")
    body = last(api, "PATCH", "/blocks/")
    assert body["paragraph"]["rich_text"][0]["text"]["content"] == "直した本文"
    await ok(tools, "delete_block", block_id=world.research.row_block)
    assert api.forwarded[-1][:2] == ("DELETE", f"/blocks/{notion_id(world.research.row_block)}")


async def test_create_database_from_short_column_types(tools, api, world):
    created = await ok(tools, "create_database", parent_id=world.research.root, title="実験",
                       properties={"名前": "title", "日付": "date", "状態": ["予定", "済み"]})
    body = last(api, "POST", "/databases")
    assert body["initial_data_source"]["properties"] == {
        "名前": {"title": {}}, "日付": {"date": {}},
        "状態": {"select": {"options": [{"name": "予定"}, {"name": "済み"}]}}}
    assert len(created["data_sources"]) == 1


async def test_update_data_source_adds_renames_and_removes_columns(tools, api, world):
    updated = await ok(tools, "update_data_source", data_source_id=world.research.ds,
                       properties={"期日": "date", "メモ": None, "名前": {"name": "タイトル"}}, title="タスク")
    body = last(api, "PATCH", "/data_sources/")
    assert body["properties"] == {"期日": {"date": {}}, "メモ": None, "名前": {"name": "タイトル"}}
    assert set(updated["schema"]) == {"タイトル", "期日"}


async def test_move_a_page_into_a_database(tools, api, world):
    await ok(tools, "move", page_id=world.research.page, new_parent_id=world.research.db)
    body = last(api, "POST", "/pages/")
    assert body == {"parent": {"type": "data_source_id", "data_source_id": notion_id(world.research.ds)}}


# 届かないもの

OUTSIDE = {
    "read": lambda w: {"target_id": w.course.page},
    "query": lambda w: {"data_source_id": w.hub.ds},
    "create_page": lambda w: {"parent_id": w.private.root, "title": "x"},
    "update_page": lambda w: {"page_id": w.course.row, "in_trash": True},
    "append_blocks": lambda w: {"target_id": w.hub.page, "content": "x"},
    "replace_content": lambda w: {"page_id": w.private.page, "content": "x"},
    "update_block": lambda w: {"block_id": w.course.row_block, "text": "x"},
    "delete_block": lambda w: {"block_id": w.hub.block},
    "create_database": lambda w: {"parent_id": w.course.root, "title": "x", "properties": {"名前": "title"}},
    "update_data_source": lambda w: {"data_source_id": w.course.ds, "title": "x"},
    "move": lambda w: {"page_id": w.research.page, "new_parent_id": w.course.root},
}


@pytest.mark.parametrize("name", sorted(OUTSIDE))
async def test_every_tool_refuses_what_is_outside_the_callers_home(tools, api, world, name):
    result = await call(tools, name, **OUTSIDE[name](world))
    assert result.is_error and "届きません" in result.content[0].text
    assert api.forwarded == []


async def test_a_relation_to_another_home_is_refused(tools, api, world):
    result = await call(tools, "create_page", parent_id=world.research.ds,
                        properties={"名前": "x", "関連": {"relation": [{"id": world.course.page}]}})
    assert result.is_error and "届きません" in result.content[0].text
    assert not any(method == "POST" and path == "/pages" for method, path, _ in api.forwarded)


async def test_the_same_tool_reaches_different_homes_per_client(tools, world):
    assert (await call(tools, "read", "course", target_id=world.course.page)).is_error is False
    assert (await call(tools, "read", "course", target_id=world.research.page)).is_error is True
    assert (await call(tools, "read", "kei-agent", target_id=world.research.page)).is_error is False


async def test_long_answers_are_cut_with_a_note(tools, api, world):
    for n in range(30):
        api.add_page(data_source=world.research.ds,
                     properties={"名前": {"title": [{"text": {"content": f"{n} " + "長" * 3000}}]}})
    found = await ok(tools, "query", data_source_id=world.research.ds)
    assert len(json.dumps(found, ensure_ascii=False)) <= service.OUTPUT_CHARS + 200
    assert "note" in found


# 本物の HTTP の上で、合言葉ごとに client が決まること


async def test_mcp_over_http_decides_the_client_from_the_token(api, world, gw_config):
    import httpx2
    import uvicorn
    from mcp.client.streamable_http import streamable_http_client

    settings = load_gateway_config(gw_config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": MASTER, "NOTION_TOKEN": "ntn_x"})
    gateway = Gateway(api, settings.roots, logging.getLogger("kei-agent-notion-gateway"))
    server = uvicorn.Server(uvicorn.Config(build_app(settings, gateway), host="127.0.0.1", port=0,
                                           log_level="warning"))
    serving = asyncio.create_task(server.serve())

    class Streams:
        def __init__(self, reader, writer):
            self.pair = (reader, writer)

        async def __aenter__(self):
            return self.pair

        async def __aexit__(self, *exc):
            return False

    async def read_as(client: str, target: str):
        headers = {"Authorization": f"Bearer {gateway_client_token(MASTER, client)}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (reader, writer),
            Client(Streams(reader, writer)) as session,
        ):
            assert {tool.name for tool in (await session.list_tools()).tools} == TOOLS
            return await session.call_tool("read", {"target_id": target})

    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        assert not (await read_as("research", world.research.page)).is_error
        assert (await read_as("research", world.course.page)).is_error
        assert not (await read_as("course", world.course.page)).is_error
        async with httpx2.AsyncClient() as http:
            master = await http.post(f"http://127.0.0.1:{port}/mcp", json={},
                                     headers={"Authorization": f"Bearer {MASTER}"})
        assert master.status_code == 401
    finally:
        server.should_exit = True
        await serving
