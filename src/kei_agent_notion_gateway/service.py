"""MCP の道具の中身。名前と形は、どの client でも同じ。

どの道具も Notion への要求を組み立てて `Gateway.request` に渡すだけなので、届く範囲の確かめ方は
中継の口（`/notion/v1`）と同じになる。任意の method と path を受け取る道具は置かない。

返事は LLM が読みやすいように短くする。プロパティは「型と値」にし、長い本文は切って、
続きの読み方（cursor）を添える。
"""

from __future__ import annotations

import json

from kei_agent.notion import append_blocks as append_in_chunks
from kei_agent.notion_store import markdown_to_blocks, plain_text, rich_text
from kei_agent_notion_gateway.gateway import ClientNotion, Gateway
from kei_agent_notion_gateway.scope import node_of, parse_id

# 1回に返す本文の文字数（続きは cursor で読む）
TEXT_CHARS = 12_000
# 1回の返事全体の上限（JSON の文字数）
OUTPUT_CHARS = 24_000
# プロパティの値1つの文字数と、一覧の件数の上限
VALUE_CHARS = 1_000
VALUE_ITEMS = 50
# 検索・絞り込み・ブロックの一覧で1回に返す件数
PAGE_SIZE = 25
# データベースを作るときに、名前だけで書いてよい列の型
SIMPLE_TYPES = frozenset({"title", "rich_text", "number", "select", "multi_select", "status", "date", "people",
                          "files", "checkbox", "url", "email", "phone_number", "created_time", "created_by",
                          "last_edited_time", "last_edited_by"})


def _cut(text: str, limit: int = VALUE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…（省略）"


def title_of(item: dict) -> str:
    """ページ・データベース・データソースの名前。"""
    if isinstance(item.get("title"), list):
        return plain_text(item["title"])
    for prop in (item.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return plain_text(prop.get("title") or [])
    return ""


def parent_of(item: dict) -> dict:
    node = node_of(item, str(item.get("object") or "page"))
    return {"type": node.parent_kind or "workspace", "id": node.parent_id}


def simple_value(prop: dict) -> object:
    """Notion のプロパティの値を、読みやすい値にする。"""
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind in ("title", "rich_text"):
        return _cut(plain_text(value or []))
    if kind in ("select", "status"):
        return (value or {}).get("name")
    if kind == "multi_select":
        return [option.get("name") for option in (value or [])][:VALUE_ITEMS]
    if kind == "date":
        return value and {key: value.get(key) for key in ("start", "end") if value.get(key)}
    if kind == "relation":
        return [ref.get("id") for ref in (value or [])][:VALUE_ITEMS]
    if kind in ("people", "created_by", "last_edited_by"):
        people = value if isinstance(value, list) else [value or {}]
        return [person.get("name") or person.get("id") for person in people][:VALUE_ITEMS]
    if kind == "files":
        return [item.get("name") for item in (value or [])][:VALUE_ITEMS]
    if kind in ("formula", "rollup"):
        inner = (value or {}).get((value or {}).get("type"))
        return inner if not isinstance(inner, list | dict) else _cut(json.dumps(inner, ensure_ascii=False))
    if kind == "unique_id":
        return f"{(value or {}).get('prefix') or ''}{'-' if (value or {}).get('prefix') else ''}{(value or {}).get('number')}"
    if isinstance(value, str):
        return _cut(value)
    return value if not isinstance(value, list | dict) else _cut(json.dumps(value, ensure_ascii=False))


def simple_properties(props: dict | None) -> dict:
    return {name: {"type": prop.get("type"), "value": simple_value(prop)}
            for name, prop in (props or {}).items() if isinstance(prop, dict)}


def simple_schema(props: dict | None) -> dict:
    """データソースの列。選択肢があれば名前を並べる。"""
    schema = {}
    for name, prop in (props or {}).items():
        kind = prop.get("type")
        entry: dict = {"type": kind}
        options = (prop.get(kind) or {}).get("options") if isinstance(prop.get(kind), dict) else None
        if options:
            entry["options"] = [option.get("name") for option in options][:VALUE_ITEMS]
        if kind == "relation":
            entry["data_source_id"] = (prop.get("relation") or {}).get("data_source_id")
        schema[name] = entry
    return schema


def property_value(name: str, kind: str | None, value: object) -> object:
    """読みやすい値を Notion の形にする。Notion の形（dict）で来たものはそのまま使う。"""
    if isinstance(value, dict):
        return value
    if kind is None:
        raise ValueError(f"「{name}」という列がありません")
    if value is None:
        return {kind: [] if kind in ("title", "rich_text", "multi_select", "relation", "people", "files") else None}
    if kind in ("title", "rich_text"):
        return {kind: rich_text(str(value))}
    if kind in ("select", "status"):
        return {kind: {"name": str(value)}}
    if kind == "multi_select":
        return {kind: [{"name": str(item)} for item in (value if isinstance(value, list) else [value])]}
    if kind == "date":
        return {kind: {"start": str(value)}}
    if kind in ("relation", "people"):
        ids = value if isinstance(value, list) else [value]
        return {kind: [{"id": parse_id(item) or str(item)} for item in ids]}
    if kind in ("number", "checkbox", "url", "email", "phone_number"):
        return {kind: value}
    raise ValueError(f"「{name}」（{kind}）は Notion の形（{{\"{kind}\": …}}）で渡してください")


def column(name: str, spec: object) -> object:
    """列の定義。型の名前、選択肢の一覧（select）、Notion の形のどれでもよい。None は列を消す。"""
    if spec is None or isinstance(spec, dict):
        return spec
    if isinstance(spec, list):
        return {"select": {"options": [{"name": str(option)} for option in spec]}}
    if isinstance(spec, str) and spec in SIMPLE_TYPES:
        return {spec: {}}
    raise ValueError(f"列「{name}」の型が分かりません: {str(spec)[:40]}")


def bounded(result: dict) -> dict:
    """返事が長すぎたら、値を短くしてから、それでも長ければ途中で切る。"""
    if len(json.dumps(result, ensure_ascii=False)) <= OUTPUT_CHARS:
        return result

    def shrink(value: object, limit: int) -> object:
        if isinstance(value, str):
            return _cut(value, limit)
        if isinstance(value, list):
            return [shrink(item, limit) for item in value[:20]]
        if isinstance(value, dict):
            return {key: item if key == "content" else shrink(item, limit) for key, item in value.items()}
        return value

    for limit in (300, 80):
        smaller = shrink(result, limit)
        if len(json.dumps(smaller, ensure_ascii=False)) <= OUTPUT_CHARS:
            return {**smaller, "note": "返事が長いので、値を途中で切りました"}
    text = json.dumps(result, ensure_ascii=False)
    return {"partial": text[:OUTPUT_CHARS], "note": "返事が長すぎるので途中で切りました。絞り込んでから読み直してください"}


class NotionTools:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    # 共通

    def _call(self, client: str, method: str, path: str, body: dict | None = None,
              query: tuple[tuple[str, str], ...] = ()) -> dict:
        return self.gateway.request(client, method, path, body, query)

    def _locate(self, client: str, value: str, kind: str | None = None) -> tuple[str, str]:
        """(ID, 種類)。ID か URL を読み、ホームの中かを確かめる（外なら ScopeError）。"""
        item_id = parse_id(value)
        if not item_id:
            raise ValueError(f"Notion の ID か URL を渡してください: {str(value)[:80]}")
        return item_id, self.gateway.scope(client).require(item_id, kind)

    def _source(self, client: str, item_id: str, kind: str) -> str:
        """データソースの ID。データベースの ID なら、その中の1つだけのデータソース。"""
        if kind == "data_source":
            return item_id
        if kind != "database":
            raise ValueError("データソース（データベース）の ID を渡してください")
        sources = self._call(client, "GET", f"/databases/{item_id}").get("data_sources") or []
        if len(sources) != 1:
            names = "、".join(f"{s.get('name')}（{s.get('id')}）" for s in sources)
            raise ValueError(f"このデータベースにはデータソースが {len(sources)} 個あります。どれか選んでください: {names}")
        return parse_id(sources[0]["id"])

    def _parent(self, client: str, value: str) -> dict:
        """ページかデータソース（データベース）を親にする書き方。"""
        item_id, kind = self._locate(client, value)
        if kind == "page":
            return {"type": "page_id", "page_id": item_id}
        if kind in ("data_source", "database"):
            return {"type": "data_source_id", "data_source_id": self._source(client, item_id, kind)}
        raise ValueError("親はページかデータソース（データベース）にしてください")

    # 読む

    def read(self, client: str, target_id: str, cursor: str = "", blocks: bool = False) -> dict:
        item_id, kind = self._locate(client, target_id)
        if kind == "data_source":
            source = self._call(client, "GET", f"/data_sources/{item_id}")
            return {"object": "data_source", "id": item_id, "title": title_of(source),
                    "database_id": parse_id((source.get("parent") or {}).get("database_id")),
                    "schema": simple_schema(source.get("properties"))}
        if kind == "database":
            database = self._call(client, "GET", f"/databases/{item_id}")
            return {"object": "database", "id": item_id, "title": title_of(database), "url": database.get("url"),
                    "parent": parent_of(database),
                    "data_sources": [{"id": parse_id(s.get("id")), "name": s.get("name")}
                                     for s in database.get("data_sources") or []]}
        if kind == "view":
            view = self._call(client, "GET", f"/views/{item_id}")
            return {"object": "view", "id": item_id, "name": view.get("name"), "type": view.get("type"),
                    "url": view.get("url"), "data_source_id": parse_id(view.get("data_source_id"))}
        if blocks or kind == "block":
            return self._blocks(client, item_id, kind, cursor)
        return self._page(client, item_id, cursor)

    def _page(self, client: str, page_id: str, cursor: str) -> dict:
        if cursor and not cursor.isdigit():
            raise ValueError("cursor には前の返事の next_cursor をそのまま渡してください")
        start = int(cursor or 0)
        page = self._call(client, "GET", f"/pages/{page_id}")
        content = self._call(client, "GET", f"/pages/{page_id}/markdown")
        text = content.get("markdown") or ""
        chunk = text[start:start + TEXT_CHARS]
        result = {"object": "page", "id": page_id, "title": title_of(page), "url": page.get("url"),
                  "parent": parent_of(page), "in_trash": bool(page.get("in_trash")),
                  "properties": simple_properties(page.get("properties")), "content": chunk}
        if start + len(chunk) < len(text):
            result["next_cursor"] = str(start + len(chunk))
            result["note"] = f"本文は {len(text)} 文字あります。続きは cursor に next_cursor を渡して読む"
        if content.get("truncated"):
            result["unknown_block_ids"] = [parse_id(i) for i in content.get("unknown_block_ids") or []][:20]
        return result

    def _blocks(self, client: str, block_id: str, kind: str, cursor: str) -> dict:
        query = (("page_size", str(PAGE_SIZE)), *((("start_cursor", cursor),) if cursor else ()))
        found = self._call(client, "GET", f"/blocks/{block_id}/children", query=query)
        items = []
        for block in found.get("results") or []:
            block_type = block.get("type")
            data = block.get(block_type) if isinstance(block.get(block_type), dict) else {}
            item = {"id": parse_id(block.get("id")), "type": block_type,
                    "text": _cut(plain_text(data.get("rich_text") or []) or str(data.get("title") or ""))}
            if "checked" in data:
                item["checked"] = data["checked"]
            if block.get("has_children"):
                item["has_children"] = True
            items.append(item)
        return {"object": kind, "id": block_id, "blocks": items,
                "next_cursor": found.get("next_cursor") if found.get("has_more") else None}

    def search(self, client: str, query: str, cursor: str = "", filter: str = "") -> dict:
        body: dict = {"query": query, "page_size": PAGE_SIZE}
        if cursor:
            body["start_cursor"] = cursor
        if filter:
            if filter not in ("page", "data_source"):
                raise ValueError("filter は page か data_source にしてください")
            body["filter"] = {"property": "object", "value": filter}
        found = self._call(client, "POST", "/search", body)
        results = [{"id": parse_id(item.get("id")), "object": item.get("object"), "title": _cut(title_of(item), 200),
                    "url": item.get("url"), "parent": parent_of(item)} for item in found.get("results") or []]
        return {"results": results, "next_cursor": found.get("next_cursor") if found.get("has_more") else None}

    def query(self, client: str, data_source_id: str, filter: dict | None = None,
              sorts: list[dict] | None = None, cursor: str = "") -> dict:
        source = self._source(client, *self._locate(client, data_source_id))
        body: dict = {"page_size": PAGE_SIZE}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["start_cursor"] = cursor
        found = self._call(client, "POST", f"/data_sources/{source}/query", body)
        rows = [{"id": parse_id(row.get("id")), "url": row.get("url"),
                 "properties": simple_properties(row.get("properties"))} for row in found.get("results") or []]
        return {"data_source_id": source, "results": rows,
                "next_cursor": found.get("next_cursor") if found.get("has_more") else None}

    # 作る・直す

    def create_page(self, client: str, parent_id: str, title: str = "", properties: dict | None = None,
                    content: str = "", icon: str = "") -> dict:
        parent = self._parent(client, parent_id)
        if parent["type"] == "page_id":
            schema = {"title": "title"}
        else:
            source = self._call(client, "GET", f"/data_sources/{parent['data_source_id']}")
            schema = {name: prop.get("type") for name, prop in (source.get("properties") or {}).items()}
        props = dict(properties or {})
        if title:
            title_name = next((name for name, kind in schema.items() if kind == "title"), "title")
            props.setdefault(title_name, title)
        body: dict = {"parent": parent,
                      "properties": {name: property_value(name, schema.get(name), value)
                                     for name, value in props.items()}}
        if content:
            body["markdown"] = content
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        page = self._call(client, "POST", "/pages", body)
        return {"id": parse_id(page.get("id")), "url": page.get("url"), "title": title_of(page)}

    def update_page(self, client: str, page_id: str, properties: dict | None = None, icon: str | None = None,
                    in_trash: bool | None = None) -> dict:
        item_id, kind = self._locate(client, page_id, "page")
        if kind != "page":
            raise ValueError("ページの ID を渡してください（ブロックは update_block、列は update_data_source）")
        body: dict = {}
        if properties:
            schema: dict = {}
            if any(not isinstance(value, dict) for value in properties.values()):
                page = self._call(client, "GET", f"/pages/{item_id}")
                schema = {name: prop.get("type") for name, prop in (page.get("properties") or {}).items()}
            body["properties"] = {name: property_value(name, schema.get(name), value)
                                  for name, value in properties.items()}
        if icon is not None:
            body["icon"] = {"type": "emoji", "emoji": icon} if icon else None
        if in_trash is not None:
            body["in_trash"] = in_trash
        if not body:
            raise ValueError("直す中身（properties / icon / in_trash）がありません")
        page = self._call(client, "PATCH", f"/pages/{item_id}", body)
        return {"id": item_id, "url": page.get("url"), "in_trash": bool(page.get("in_trash"))}

    def append_blocks(self, client: str, target_id: str, content: str = "", blocks: list[dict] | None = None,
                      after: str = "") -> dict:
        item_id, _kind = self._locate(client, target_id)
        children = [*markdown_to_blocks(content), *(blocks or [])]
        if not children:
            raise ValueError("足す中身（content か blocks）がありません")
        after_id = parse_id(after) if after else None
        if after and not after_id:
            raise ValueError("after にはブロックの ID を渡してください")
        added = append_in_chunks(ClientNotion(self.gateway, client), item_id, children, after_id)
        return {"id": item_id, "added": len(added), "block_ids": [parse_id(b.get("id")) for b in added][:VALUE_ITEMS]}

    def replace_content(self, client: str, page_id: str, content: str) -> dict:
        item_id, kind = self._locate(client, page_id, "page")
        if kind != "page":
            raise ValueError("本文を置き換えられるのはページだけです")
        # 子ページとデータベースは消さない（allow_deleting_content は渡さない）
        result = self._call(client, "PATCH", f"/pages/{item_id}/markdown",
                            {"type": "replace_content", "replace_content": {"new_str": content}})
        return {"id": item_id, "characters": len(result.get("markdown") or ""), "truncated": bool(result.get("truncated"))}

    def update_block(self, client: str, block_id: str, text: str | None = None, block: dict | None = None) -> dict:
        item_id, _kind = self._locate(client, block_id, "block")
        if text is None and not block:
            raise ValueError("直す中身（text か block）がありません")
        body = dict(block or {})
        if text is not None:
            current = self._call(client, "GET", f"/blocks/{item_id}")
            kind = current.get("type")
            if not isinstance(current.get(kind), dict) or "rich_text" not in current[kind]:
                raise ValueError(f"{kind} のブロックは text で直せません。block に Notion の形で渡してください")
            body[kind] = {**(body.get(kind) or {}), "rich_text": rich_text(text)}
        updated = self._call(client, "PATCH", f"/blocks/{item_id}", body)
        return {"id": item_id, "type": updated.get("type")}

    def delete_block(self, client: str, block_id: str) -> dict:
        item_id, _kind = self._locate(client, block_id, "block")
        self._call(client, "DELETE", f"/blocks/{item_id}")
        return {"id": item_id, "in_trash": True}

    def create_database(self, client: str, parent_id: str, title: str, properties: dict,
                        description: str = "") -> dict:
        item_id, kind = self._locate(client, parent_id, "page")
        if kind != "page":
            raise ValueError("データベースはページの下に作ります")
        body: dict = {"parent": {"type": "page_id", "page_id": item_id}, "title": rich_text(title),
                      "initial_data_source": {"properties": {
                          name: column(name, spec) for name, spec in properties.items() if spec is not None}}}
        if description:
            body["description"] = rich_text(description)
        database = self._call(client, "POST", "/databases", body)
        return {"id": parse_id(database.get("id")), "url": database.get("url"),
                "data_sources": [{"id": parse_id(s.get("id")), "name": s.get("name")}
                                 for s in database.get("data_sources") or []]}

    def update_data_source(self, client: str, data_source_id: str, properties: dict | None = None,
                           title: str | None = None) -> dict:
        source = self._source(client, *self._locate(client, data_source_id))
        body: dict = {}
        if properties:
            body["properties"] = {name: column(name, spec) for name, spec in properties.items()}
        if title is not None:
            body["title"] = rich_text(title)
        if not body:
            raise ValueError("直す中身（properties か title）がありません")
        updated = self._call(client, "PATCH", f"/data_sources/{source}", body)
        return {"id": source, "title": title_of(updated), "schema": simple_schema(updated.get("properties"))}

    def move(self, client: str, page_id: str, new_parent_id: str) -> dict:
        item_id, kind = self._locate(client, page_id, "page")
        if kind != "page":
            raise ValueError("移せるのはページだけです（Notion の API の制約）")
        moved = self._call(client, "POST", f"/pages/{item_id}/move", {"parent": self._parent(client, new_parent_id)})
        return {"id": item_id, "url": moved.get("url"), "parent": parent_of(moved)}
