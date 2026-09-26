"""Notion ゲートウェイ: client ごとの合言葉、届くホーム、中継の口（/notion/v1）。

本物の Notion には届かない。ゲートウェイの後ろには手元だけの Notion（fakes.FakeNotionAPI）を置く。
MCP の道具は test_notion_gateway_mcp.py。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fakes import FakeNotionAPI

from kei_agent import notion as notion_module
from kei_agent.config import NotionConfig, notion_id
from kei_agent.notion import Notion, NotionError, gateway_client_token
from kei_agent_notion_gateway.app import build_app
from kei_agent_notion_gateway.clients import Tokens, client_roots
from kei_agent_notion_gateway.config import load_gateway_config
from kei_agent_notion_gateway.gateway import Gateway
from kei_agent_notion_gateway.rules import Refused, plan, references
from kei_agent_notion_gateway.scope import Scope, ScopeError, Tree

MASTER = "test-master"
LOGGER = "kei-agent-notion-gateway"


def auth(client: str) -> dict:
    return {"Authorization": f"Bearer {gateway_client_token(MASTER, client)}"}


def area(api: FakeNotionAPI, root: str) -> SimpleNamespace:
    """ホーム（か、どのホームでもない場所）の中の、よく触る一式。"""
    db, ds = api.add_database(root, "Task", {"名前": {"title": {}}, "メモ": {"rich_text": {}}})
    row = api.add_page(data_source=ds, properties={"名前": {"title": [{"text": {"content": "行"}}]}})
    return SimpleNamespace(root=root, db=db, ds=ds, row=row, page=api.add_page(root, "メモ"),
                           block=api.add_block(root, "heading_2", "見出し"),
                           row_block=api.add_block(row, "paragraph", "本文"), view=api.add_view(db, "表"))


@pytest.fixture
def api():
    return FakeNotionAPI()


@pytest.fixture
def world(api):
    """共通・研究・授業の3つのホームと、どのホームでもない場所（private）。"""
    roots = {name: api.add_page(None, title) for name, title in
             (("hub", "Keitaro Ueki"), ("research", "研究ホーム"), ("course", "授業ホーム"), ("private", "日記"))}
    return SimpleNamespace(**{name: area(api, root) for name, root in roots.items()})


@pytest.fixture
def gw_config(config, world):
    return replace(config, notion=NotionConfig(hub_home=notion_id(world.hub.root),
                                               research_home=notion_id(world.research.root),
                                               course_home=notion_id(world.course.root)))


@pytest.fixture
def gateway(api, gw_config):
    return Gateway(api, client_roots(gw_config.notion), logging.getLogger(LOGGER))


@pytest.fixture
def settings(gw_config):
    return load_gateway_config(gw_config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": MASTER, "NOTION_TOKEN": "ntn_test"})


@pytest.fixture
def app(settings, gateway):
    return build_app(settings, gateway)


@pytest.fixture
async def http(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        yield client


# 設定と合言葉


def test_gateway_needs_the_master_and_the_notion_token(gw_config):
    with pytest.raises(RuntimeError, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        load_gateway_config(gw_config, {"NOTION_TOKEN": "notion"})
    with pytest.raises(RuntimeError, match="NOTION_TOKEN"):
        load_gateway_config(gw_config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "gateway"})


def test_gateway_does_not_start_without_homes(config):
    with pytest.raises(RuntimeError, match=r"\[notion\]"):
        load_gateway_config(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": MASTER, "NOTION_TOKEN": "notion"})


def test_homes_come_from_config_toml_not_from_notion_json(settings, world, gw_config):
    homes = {name: notion_id(getattr(world, name).root) for name in ("hub", "research", "course")}
    assert (settings.host, settings.port) == ("127.0.0.1", 8791)
    assert settings.roots == {"research": {homes["research"]}, "course": {homes["course"]},
                              "kei-agent": set(homes.values())}
    assert not (gw_config.state_dir / "notion.json").exists()


def test_home_ids_are_compared_without_dashes_or_case():
    roots = client_roots(NotionConfig(research_home="3DE4FB5D-2D07-80B9-A193-F4604D5EA09C"))
    assert roots == {"research": {"3de4fb5d2d0780b9a193f4604d5ea09c"}, "course": frozenset(),
                     "kei-agent": {"3de4fb5d2d0780b9a193f4604d5ea09c"}}


def test_only_client_tokens_are_accepted_never_the_master():
    tokens = Tokens(MASTER)
    for client in ("kei-agent", "research", "course"):
        assert tokens.client(f"Bearer {gateway_client_token(MASTER, client)}") == client
    for header in (f"Bearer {MASTER}", "Bearer nope", "", f"Basic {gateway_client_token(MASTER, 'course')}",
                   f"Bearer {gateway_client_token('another-master', 'course')}", "Bearer ✗"):
        assert tokens.client(header) is None


async def test_health_is_open_and_everything_else_needs_a_client_token(http):
    assert (await http.get("/health")).status_code == 200
    for headers in ({}, {"Authorization": f"Bearer {MASTER}"}, {"Authorization": "Bearer nope"}):
        for path in ("/mcp", "/notion/v1/search"):
            response = await http.post(path, json={}, headers=headers)
            assert response.status_code == 401
            assert response.json()["object"] == "error"


# 届くホーム


def test_scope_accepts_the_home_and_its_descendants_in_any_id_form(world, gateway):
    scope = gateway.scope("research")
    home = world.research
    assert scope.require(home.root) == "page"
    assert scope.require(home.row_block) == "block"
    assert scope.require(home.row) == "page"
    assert scope.require(home.ds) == "data_source"
    assert scope.require(home.db) == "database"
    assert scope.require(home.view) == "view"
    assert scope.require(notion_id(home.page).upper()) == "page"
    assert scope.require(f"https://www.notion.so/Cafe-{notion_id(home.page)}?pvs=4") == "page"


@pytest.mark.parametrize("other", ["private", "course", "hub"])
def test_scope_refuses_the_other_homes(world, gateway, other):
    with pytest.raises(ScopeError) as refused:
        gateway.scope("research").require(getattr(world, other).row_block)
    assert refused.value.item_id == notion_id(getattr(world, other).row_block)


def test_scope_refuses_unknown_ids_cycles_and_depth(api, world, gateway):
    scope = gateway.scope("research")
    with pytest.raises(ScopeError, match="見つかりません"):
        scope.require("0" * 32)
    with pytest.raises(ScopeError, match="読めません"):
        scope.require("not-an-id")
    first, second = api.add_page(world.private.root, "a"), api.add_page(world.private.root, "b")
    api.items[api.key(first)]["parent"] = {"type": "page_id", "page_id": second}
    api.items[api.key(second)]["parent"] = {"type": "page_id", "page_id": first}
    with pytest.raises(ScopeError, match="循環"):
        scope.require(first)
    shallow = Scope("research", frozenset({notion_id(world.research.root)}), Tree(api), max_depth=1)
    with pytest.raises(ScopeError, match="深すぎ"):
        shallow.require(world.research.row_block)


def test_a_refusal_names_only_the_requested_id(api, world, gateway):
    """たどった先の祖先（共有されていないページ）の ID は、断る文に出さない。"""
    hidden = api.add_page(world.private.root, "共有していない")
    child = api.add_page(hidden, "子")
    del api.items[api.key(hidden)]
    with pytest.raises(ScopeError) as refused:
        gateway.scope("research").require(child)
    assert refused.value.item_id == notion_id(child)


def test_scope_remembers_parents_for_sixty_seconds(api, world):
    now = [0.0]
    scope = Scope("research", frozenset({notion_id(world.research.root)}), Tree(api, clock=lambda: now[0]))
    scope.require(world.research.row_block)
    looked_up = len(api.lookups)
    scope.require(world.research.row_block)
    assert len(api.lookups) == looked_up
    now[0] = 61.0
    scope.require(world.research.row_block)
    assert len(api.lookups) > looked_up


def test_a_notion_outage_is_not_read_as_outside(api, world, gateway):
    def down(method, path, body=None):
        raise NotionError("503 unavailable", 503)

    api.request = down
    with pytest.raises(NotionError):
        gateway.scope("research").require(world.research.page)


# 要求の読み方


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/users"), ("GET", "/users/me"), ("POST", "/comments"), ("GET", "/comments"),
    ("POST", "/file_uploads"), ("GET", "/async_tasks/" + "a" * 32), ("POST", "/databases/" + "a" * 32 + "/query"),
    ("GET", "/pages/" + "a" * 32 + "/properties/title"), ("PUT", "/pages/" + "a" * 32), ("GET", "/pages/../users"),
    ("POST", "/views/" + "a" * 32 + "/queries"), ("GET", "/blocks/not-an-id"), ("GET", "/views"),
])
def test_rules_refuse_what_the_gateway_does_not_understand(method, path):
    with pytest.raises(Refused):
        plan(method, path)


def test_rules_collect_every_reference_in_a_body():
    body = {
        "parent": {"type": "data_source_id", "data_source_id": "D"},
        "template": {"type": "template_id", "template_id": "T"},
        "properties": {"科目": {"relation": [{"id": "R"}]}, "メモ": {"rich_text": [
            {"type": "mention", "mention": {"type": "page", "page": {"id": "M"}}},
            {"type": "mention", "mention": {"type": "user", "user": {"id": "U"}}}]}},
        "children": [
            {"type": "link_to_page", "link_to_page": {"type": "page_id", "page_id": "L"}},
            {"type": "synced_block", "synced_block": {"synced_from": {"type": "block_id", "block_id": "S"}}}],
        "position": {"type": "after_block", "after_block": {"id": "A"}},
    }
    assert set(references(body)) == {("D", "data_source"), ("T", "page"), ("R", "page"), ("M", "page"),
                                     ("L", "page"), ("S", "block"), ("A", "block")}


def test_rules_find_page_references_in_notion_markdown():
    page, child, user = "3de4fb5d2d0780b9a193f4604d5ea09c", "1" * 32, "2" * 32
    text = (f'中身 <mention-page url="https://www.notion.so/Cafe-{page}">メモ</mention-page>\n'
            f'<page url="https://notion.so/{child}">子</page> <mention-user url="user://{user}">人</mention-user>')
    found = plan("POST", "/pages", body={"parent": {"type": "page_id", "page_id": page}, "markdown": text}).targets
    assert set(found) == {(page, "page"), (child, "page")}


def test_rules_refuse_the_workspace_and_missing_parents():
    for body in ({"parent": {"type": "workspace", "workspace": True}}, {"properties": {}}):
        with pytest.raises(Refused):
            plan("POST", "/pages", body=body)
    with pytest.raises(Refused):
        plan("PATCH", "/databases/" + "a" * 32, body={"parent": {"type": "workspace", "workspace": True}})


# 中継の口: 決まった処理（src/kei_agent・src/kei_agent_course）と MCP の道具が使う形

TITLE = {"title": [{"type": "text", "text": {"content": "x"}}]}
HEADING = {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "見出し"}}]}}

SHAPES = {
    "children": lambda a: ("GET", f"/blocks/{a.root}/children", None, [("page_size", "100")]),
    "child_page": lambda a: ("POST", "/pages", {"parent": {"type": "page_id", "page_id": a.root},
                                                "icon": {"type": "emoji", "emoji": "🧭"}, "properties": {"title": TITLE}}),
    "create_database": lambda a: ("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": a.root}, "title": [{"text": {"content": "DB"}}],
        "initial_data_source": {"properties": {"名前": {"title": {}}, "関連": {"relation": {
            "data_source_id": a.ds, "type": "dual_property", "dual_property": {"synced_property_name": "逆"}}}}}}),
    "get_database": lambda a: ("GET", f"/databases/{a.db}", None),
    "rename_database": lambda a: ("PATCH", f"/databases/{a.db}", {"title": [{"text": {"content": "予定カレンダー"}}]}),
    "get_data_source": lambda a: ("GET", f"/data_sources/{a.ds}", None),
    "update_data_source": lambda a: ("PATCH", f"/data_sources/{a.ds}", {"properties": {"メモ2": {"rich_text": {}}}}),
    "templates": lambda a: ("GET", f"/data_sources/{a.ds}/templates", None),
    "query": lambda a: ("POST", f"/data_sources/{a.ds}/query", {
        "page_size": 100, "filter": {"property": "名前", "title": {"equals": "行"}}}),
    "create_row": lambda a: ("POST", "/pages", {
        "parent": {"type": "data_source_id", "data_source_id": a.ds},
        "properties": {"名前": TITLE, "科目": {"relation": [{"id": a.page}]}}, "children": [HEADING]}),
    "get_page": lambda a: ("GET", f"/pages/{a.page}", None),
    "update_row": lambda a: ("PATCH", f"/pages/{a.row}", {"properties": {"名前": TITLE}}),
    "home_icon": lambda a: ("PATCH", f"/pages/{a.root}", {"icon": {"type": "emoji", "emoji": "🔬"}}),
    "trash_page": lambda a: ("PATCH", f"/pages/{a.row}", {"in_trash": True}),
    "append": lambda a: ("PATCH", f"/blocks/{a.root}/children", {"children": [HEADING]}),
    "append_after": lambda a: ("PATCH", f"/blocks/{a.row}/children", {
        "children": [HEADING], "position": {"type": "after_block", "after_block": {"id": a.row_block}}}),
    "row_children": lambda a: ("GET", f"/blocks/{a.row}/children", None),
    "trash_block": lambda a: ("PATCH", f"/blocks/{a.block}", {"in_trash": True}),
    "move": lambda a: ("POST", f"/pages/{a.row}/move", {"parent": {"type": "page_id", "page_id": a.page}}),
    "views_of_database": lambda a: ("GET", "/views", None, [("database_id", a.db)]),
    "views_of_source": lambda a: ("GET", "/views", None, [("data_source_id", a.ds)]),
    "get_view": lambda a: ("GET", f"/views/{a.view}", None),
    "update_view": lambda a: ("PATCH", f"/views/{a.view}", {
        "configuration": {"type": "table", "properties": [{"property_id": "title", "visible": True}]}}),
    "create_view": lambda a: ("POST", "/views", {"database_id": a.db, "data_source_id": a.ds, "name": "今夜",
                                                 "type": "table"}),
    "linked_view": lambda a: ("POST", "/views", {
        "data_source_id": a.ds, "name": "自分の Task", "type": "table",
        "create_database": {"parent": {"type": "page_id", "page_id": a.root},
                            "position": {"type": "after_block", "block_id": a.block}}}),
    "get_markdown": lambda a: ("GET", f"/pages/{a.page}/markdown", None),
    "replace_markdown": lambda a: ("PATCH", f"/pages/{a.page}/markdown", {
        "type": "replace_content", "replace_content": {"new_str": "# 新しい本文"}}),
    "get_block": lambda a: ("GET", f"/blocks/{a.row_block}", None),
    "update_block": lambda a: ("PATCH", f"/blocks/{a.row_block}", {
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "直した"}}]}}),
    "delete_block": lambda a: ("DELETE", f"/blocks/{a.row_block}", None),
}


async def send(http, client: str, shape, home: SimpleNamespace):
    method, path, body, *query = shape(home)
    return await http.request(method, "/notion/v1" + path, params=query[0] if query else None,
                              content=json.dumps(body) if body is not None else None, headers=auth(client))


@pytest.mark.parametrize("name", sorted(SHAPES))
@pytest.mark.parametrize(("client", "inside", "outside"), [("course", "course", "research"),
                                                           ("kei-agent", "hub", "private")])
async def test_each_request_shape_goes_through_inside_and_is_refused_outside(http, api, world, name, client,
                                                                          inside, outside):
    allowed = await send(http, client, SHAPES[name], getattr(world, inside))
    assert allowed.status_code == 200, allowed.text
    assert len(api.forwarded) == 1

    api.forwarded.clear()
    refused = await send(http, client, SHAPES[name], getattr(world, outside))
    error = refused.json()
    assert refused.status_code == 403 and error["object"] == "error" and error["status"] == 403
    assert error["code"] == "restricted_resource"
    assert error["message"].startswith(f"Kei Agent gateway: {client} can't reach ")
    assert api.forwarded == []


@pytest.mark.parametrize("home", ["hub", "research", "course"])
async def test_kei_agent_reaches_all_three_homes(http, world, home):
    for name in ("get_page", "query", "create_row", "append", "linked_view"):
        assert (await send(http, "kei-agent", SHAPES[name], getattr(world, home))).status_code == 200


async def test_research_gets_no_raw_proxy_even_inside_its_home(http, api, world):
    refused = await send(http, "research", SHAPES["get_page"], world.research)
    assert refused.status_code == 403
    assert refused.json()["message"] == "Kei Agent gateway: research can't use the Notion API proxy"
    assert api.forwarded == []


def _mention(page):
    return {"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "mention", "mention": {"type": "page", "page": {"id": page}}}]}}


OUTSIDE_REFERENCES = {
    "relation": lambda w: ("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": w.course.ds},
                                              "properties": {"科目": {"relation": [{"id": w.research.page}]}}}),
    "template_on_create": lambda w: ("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": w.course.root},
        "template": {"type": "template_id", "template_id": w.hub.page}}),
    "template_on_update": lambda w: ("PATCH", f"/pages/{w.course.row}", {
        "template": {"type": "template_id", "template_id": w.private.page}}),
    "mention": lambda w: ("PATCH", f"/blocks/{w.course.root}/children", {"children": [_mention(w.private.page)]}),
    "link_to_page": lambda w: ("PATCH", f"/blocks/{w.course.root}/children", {"children": [
        {"type": "link_to_page", "link_to_page": {"type": "page_id", "page_id": w.hub.page}}]}),
    "synced_block": lambda w: ("PATCH", f"/blocks/{w.course.root}/children", {"children": [
        {"type": "synced_block", "synced_block": {"synced_from": {"type": "block_id", "block_id": w.hub.block}}}]}),
    "after_block": lambda w: ("PATCH", f"/blocks/{w.course.row}/children", {
        "children": [HEADING], "position": {"type": "after_block", "after_block": {"id": w.research.block}}}),
    "markdown_on_create": lambda w: ("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": w.course.root},
        "markdown": f'<mention-page url="https://www.notion.so/{notion_id(w.private.page)}">日記</mention-page>'}),
    "markdown_on_replace": lambda w: ("PATCH", f"/pages/{w.course.page}/markdown", {
        "type": "replace_content",
        "replace_content": {"new_str": f'<page url="https://www.notion.so/{notion_id(w.research.page)}">子</page>'}}),
    "move_out": lambda w: ("POST", f"/pages/{w.course.row}/move", {
        "parent": {"type": "page_id", "page_id": w.research.page}}),
    "move_in": lambda w: ("POST", f"/pages/{w.research.row}/move", {
        "parent": {"type": "page_id", "page_id": w.course.page}}),
    "linked_view_of_outside_source": lambda w: ("POST", "/views", {
        "data_source_id": w.research.ds, "name": "盗み見", "type": "table",
        "create_database": {"parent": {"type": "page_id", "page_id": w.course.root}}}),
    "relation_column_to_outside": lambda w: ("PATCH", f"/data_sources/{w.course.ds}", {"properties": {"逆": {
        "relation": {"data_source_id": w.research.ds, "type": "dual_property",
                     "dual_property": {"synced_property_name": "授業"}}}}}),
    "database_moved_out": lambda w: ("PATCH", f"/databases/{w.course.db}", {
        "parent": {"type": "page_id", "page_id": w.research.root}}),
    "database_to_workspace": lambda w: ("PATCH", f"/databases/{w.course.db}", {
        "parent": {"type": "workspace", "workspace": True}}),
    "page_in_workspace": lambda w: ("POST", "/pages", {"parent": {"type": "workspace", "workspace": True},
                                                       "properties": {"title": TITLE}}),
    "page_without_parent": lambda w: ("POST", "/pages", {"properties": {"title": TITLE}}),
}


@pytest.mark.parametrize("name", sorted(OUTSIDE_REFERENCES))
async def test_a_reference_outside_the_home_is_refused_even_under_an_inside_target(http, api, world, name):
    method, path, body = OUTSIDE_REFERENCES[name](world)
    refused = await http.request(method, "/notion/v1" + path, content=json.dumps(body), headers=auth("course"))
    assert refused.status_code == 403 and refused.json()["code"] == "restricted_resource"
    assert api.forwarded == []


def linked_database(api: FakeNotionAPI, page: str, source: str) -> str:
    """page の中に置いた、source（ほかの場所のデータソース）を見せるリンクドデータベース。"""
    _status, data, _headers = api._handle("POST", "/views", {
        "data_source_id": source, "name": "リンク", "type": "table",
        "create_database": {"parent": {"type": "page_id", "page_id": page}}})
    return json.loads(data)["parent"]["database_id"]


async def test_a_linked_database_that_shows_another_home_counts_as_outside(http, api, world):
    """ホームの中に置いても、中身が外なら通さない（そこへの作成・移動は外のデータソースに届く）。"""
    linked = linked_database(api, world.course.root, world.research.ds)
    for method, path, body in (
        ("POST", f"/pages/{world.course.page}/move", {"parent": {"type": "page_id", "page_id": linked}}),
        ("POST", "/pages", {"parent": {"type": "database_id", "database_id": linked}, "properties": {"名前": TITLE}}),
        ("GET", f"/databases/{linked}", None),
        ("GET", "/views", None),
    ):
        params = [("database_id", linked)] if path == "/views" else None
        refused = await http.request(method, "/notion/v1" + path, params=params, headers=auth("course"),
                                     content=json.dumps(body) if body is not None else None)
        assert refused.status_code == 403, (method, path)
    assert api.forwarded == []
    # 本体は研究ホームにも届くので、同じリンクドデータベースを読める
    assert (await http.get(f"/notion/v1/databases/{linked}", headers=auth("kei-agent"))).status_code == 200


@pytest.mark.parametrize(("method", "path"), [("GET", "/users"), ("GET", "/users/me"), ("POST", "/comments"),
                                              ("POST", "/file_uploads"), ("GET", "/async_tasks/" + "a" * 32)])
async def test_the_proxy_refuses_paths_it_does_not_understand(http, api, method, path):
    refused = await http.request(method, "/notion/v1" + path, headers=auth("kei-agent"))
    assert refused.status_code == 403 and refused.json()["code"] == "restricted_resource"
    assert api.forwarded == []


async def test_search_returns_only_results_inside_the_home(http, world):
    found = await http.post("/notion/v1/search", json={"query": ""}, headers=auth("course"))
    course = world.course
    assert {notion_id(item["id"]) for item in found.json()["results"]} == {
        notion_id(course.root), notion_id(course.page), notion_id(course.row), notion_id(course.ds)}


async def test_notion_status_body_and_retry_after_come_back_verbatim(http, api, world):
    limited = b'{"object":"error","status":429,"code":"rate_limited","message":"slow down"}'
    api.fail_next = [(429, limited, {"Retry-After": "7"})]
    response = await send(http, "course", SHAPES["get_page"], world.course)
    assert response.status_code == 429 and response.headers["Retry-After"] == "7"
    assert response.content == limited


async def test_a_lost_connection_is_retryable_only_when_nothing_was_written(http, api, world):
    api.fail_next = [ConnectionResetError("reset")]
    assert (await send(http, "course", SHAPES["get_page"], world.course)).status_code == 502
    api.fail_next = [ConnectionResetError("reset")]
    lost = await send(http, "course", SHAPES["create_row"], world.course)
    # 5xx にすると呼んだ側が送り直し、二重に作ってしまう
    assert lost.status_code == 409 and "書き込みが済んだか分かりません" in lost.json()["message"]


async def test_a_notion_outage_while_checking_sends_nothing(http, api, world):
    def down(method, path, body=None):
        raise NotionError("503 unavailable", 503)

    api.request = down
    response = await send(http, "course", SHAPES["get_page"], world.course)
    assert response.status_code == 503 and api.forwarded == []


async def test_a_body_that_is_not_json_is_refused(http, api):
    response = await http.post("/notion/v1/pages", content=b"{", headers=auth("course"))
    assert response.status_code == 400 and api.forwarded == []


async def test_a_moved_page_is_checked_again_at_its_new_place(http, world, gateway):
    page = world.course.page
    assert gateway.scope("course").require(page) == "page"
    moved = await http.post(f"/notion/v1/pages/{page}/move", headers=auth("kei-agent"),
                            json={"parent": {"type": "page_id", "page_id": world.research.root}})
    assert moved.status_code == 200
    with pytest.raises(ScopeError):
        gateway.scope("course").require(page)


async def test_the_log_has_client_operation_and_target_but_no_contents(http, world, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await http.post("/notion/v1/pages", headers=auth("course"), json={
            "parent": {"type": "page_id", "page_id": world.course.root},
            "properties": {"title": {"title": [{"text": {"content": "do-not-log"}}]}}})
        await send(http, "course", SHAPES["get_page"], world.research)
    assert f"course POST /pages {notion_id(world.course.root)}" in caplog.text
    assert f"course GET /pages/{{id}} {notion_id(world.research.page)} 失敗（scope）" in caplog.text
    for secret in ("do-not-log", MASTER, gateway_client_token(MASTER, "course")):
        assert secret not in caplog.text


# 本物の HTTP の上で、決まった処理のコードがそのまま動くこと


@pytest.fixture
def served(app):
    """ゲートウェイを別のスレッドで本物の HTTP として立てる（決まった処理は urllib で呼ぶ）。"""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def via(served, monkeypatch):
    """client の合言葉でゲートウェイを呼ぶ Notion（kei_agent.notion.gateway_notion と同じ作り）。"""
    monkeypatch.setattr(notion_module, "MIN_INTERVAL_SECONDS", 0)
    return lambda client: Notion(gateway_client_token(MASTER, client), base_url=f"{served}/notion/v1")


def test_course_setup_and_sync_run_through_the_gateway(via, world, tmp_path):
    from kei_agent_course import notion_setup, notion_sync
    from kei_agent_course.ics import Event

    course = via("course")
    setup = notion_setup.CourseSetup(course, world.course.root, tmp_path / "notion-course.json")
    setup.run(notion_setup.AUTUMN_2026[:1], year=2026)
    assert set(setup.state["databases"]) == {"courses", "assignments", "grades", "requirements", "gpa"}

    event = Event(uid="1@moodle", summary="第3回レポート の 提出期限", starts_at=datetime(2026, 9, 25, 23, 59),
                  course="データベース(2019ZZ26)")
    result = notion_sync.CourseNotion(course, setup.state).sync([event])
    assert len(result.added) == 1 and not result.other_courses

    with pytest.raises(NotionError) as refused:
        course.request("GET", f"/pages/{world.research.page}")
    assert refused.value.status == 403 and "course can't reach" in str(refused.value)


def test_research_setup_and_tasks_run_through_the_gateway(via, world, tmp_path):
    from kei_agent.notion import Setup
    from kei_agent.notion_store import NotionStore

    kei = via("kei-agent")
    Setup(kei, world.research.root, tmp_path / "notion.json").run()
    store = NotionStore(kei, tmp_path / "notion.json")
    assert store.ensure_theme("vlm", "https://slack.example/c", "~/research/vlm")
    task = store.create_night_task("試す", "vlm", "https://slack.example/p1", "本文")
    assert [found.id for found in store.tonight_tasks(5)] == [task.id]
    assert task.theme_names == ["vlm"]

    with pytest.raises(NotionError) as refused:
        via("research").request("GET", f"/pages/{world.research.page}")
    assert refused.value.status == 403


def test_papers_are_filed_once_and_every_theme_page_shows_its_own(via, api, world, tmp_path):
    """同じ論文は1行に、関係するテーマを並べる。テーマのページには、そのテーマの論文だけの表を1つ置く。"""
    from kei_agent.notion import THEME_PAPERS_VIEW, Setup
    from kei_agent.notion_store import NotionStore

    kei = via("kei-agent")
    state = tmp_path / "notion.json"
    Setup(kei, world.research.root, state).run()
    store = NotionStore(kei, state)
    store.ensure_theme("vlm", "https://slack.example/c1", "~/research/vlm")
    store.ensure_theme("amr", "https://slack.example/c2", "~/research/amr")
    paper = {"id": "arXiv:2609.00001", "title": "Counting with VLMs", "url": "https://arxiv.org/abs/2609.00001",
             "authors": ["A. Author"], "year": "2026", "venue": "", "summary": "要点", "relation": "関係"}

    assert store.add_papers("vlm", [paper], "毎朝の新着") == 1
    assert store.add_papers("amr", [paper], "依頼") == 1          # 同じ行にテーマを足す
    assert store.add_papers("amr", [paper], "依頼") == 0
    assert store.paper_ids() == ["arXiv:2609.00001"]

    Setup(kei, world.research.root, state).run()                   # 作り直しても表は増えない
    shown = [item for item in api.items.values() if item["object"] == "view" and item["name"] == THEME_PAPERS_VIEW]
    assert len(shown) == 2
