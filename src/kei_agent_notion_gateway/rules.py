"""Notion の API への要求1つが、どの ID に触れるか。

ここに書いた形だけを通す。分からない形（`/users`、`/comments`、`/file_uploads` など）は断る。
ID を拾うのはパス、クエリ、本文のすべて。本文は親だけでなく、テンプレート、リレーション、
メンション、ページへのリンク、同期ブロック、位置の指定、Markdown のページ参照まで見る
（どれも、ホームの外のページを読み出したり書き換えたりする入口になる）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from kei_agent_notion_gateway.scope import parse_id

# 本文の中で、その値が Notion の ID を指すキー
_ID_KEYS = {"page_id": "page", "block_id": "block", "database_id": "database", "data_source_id": "data_source",
            "view_id": "view", "template_id": "page"}
# メンションの種類 → ID の種類（人・日付・リンクは ID でたどらない）
_MENTIONS = {"page": "page", "database": "database", "data_source": "data_source"}
# Markdown の中でページやブロックを指すタグ（`<page url="…">` など）
_MARKDOWN_TAGS = {"page": "page", "database": "database", "synced_block": "block", "synced_block_reference": "block",
                  "mention-page": "page", "mention-database": "database", "mention-data-source": "data_source",
                  "unknown": "block"}
_MARKDOWN_REF = re.compile(r"<(" + "|".join(sorted(_MARKDOWN_TAGS, key=len, reverse=True)) +
                           r")\b[^>]*?\burl\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
# Markdown を書き込む要求と、Markdown が入るキー
_MARKDOWN_KEYS = {"POST /pages": frozenset({"markdown"}),
                  "PATCH /pages/{id}/markdown": frozenset({"new_str", "content"})}
# クエリで ID を渡すキー（ビューの一覧）
_QUERY_KEYS = {"database_id": "database", "data_source_id": "data_source"}
_SEGMENT = re.compile(r"[0-9a-fA-F-]{32,36}")


class Refused(ValueError):
    """ゲートウェイが分からない・受け付けない要求。"""


@dataclass(frozen=True)
class Plan:
    # 記録に残す操作の名前（"PATCH /pages/{id}" など。ID や中身は入れない）
    operation: str
    # 送る前に確かめる (ID, 種類)。最初のものを記録の対象にする
    targets: tuple[tuple[str, str], ...]
    # 検索の結果を、届くものだけに絞る
    search: bool = False
    # 成功したら親の覚え書きを捨てる ID（移動したもの）
    moved: tuple[str, ...] = ()


def _id_segment(segment: str) -> str:
    if not _SEGMENT.fullmatch(segment) or not parse_id(segment):
        raise Refused(f"Notion の ID として読めません: {segment[:40]}")
    return segment


def _route(method: str, parts: list[str]) -> tuple[str, str, str]:
    """(操作の名前, パスの ID, その種類)。ID のない形は ID を空にする。"""
    shapes = {
        # (method, 1段目, 3段目) → 種類。2段目が ID
        ("GET", "pages", ""): "page", ("PATCH", "pages", ""): "page",
        ("POST", "pages", "move"): "page",
        ("GET", "pages", "markdown"): "page", ("PATCH", "pages", "markdown"): "page",
        ("GET", "blocks", ""): "block", ("PATCH", "blocks", ""): "block", ("DELETE", "blocks", ""): "block",
        ("GET", "blocks", "children"): "block", ("PATCH", "blocks", "children"): "block",
        ("GET", "databases", ""): "database", ("PATCH", "databases", ""): "database",
        ("GET", "data_sources", ""): "data_source", ("PATCH", "data_sources", ""): "data_source",
        ("POST", "data_sources", "query"): "data_source", ("GET", "data_sources", "templates"): "data_source",
        ("GET", "views", ""): "view", ("PATCH", "views", ""): "view", ("DELETE", "views", ""): "view",
    }
    creates = {("POST", "pages"), ("POST", "databases"), ("POST", "data_sources"), ("POST", "views"),
               ("GET", "views"), ("POST", "search")}
    if len(parts) == 1 and (method, parts[0]) in creates:
        return f"{method} /{parts[0]}", "", ""
    if len(parts) in (2, 3):
        kind = shapes.get((method, parts[0], parts[2] if len(parts) == 3 else ""))
        if kind:
            tail = f"/{parts[2]}" if len(parts) == 3 else ""
            return f"{method} /{parts[0]}/{{id}}{tail}", _id_segment(parts[1]), kind
    raise Refused(f"ゲートウェイが扱わない要求です: {method} /{'/'.join(parts[:1])}")


def plan(method: str, path: str, query: Sequence[tuple[str, str]] = (), body: object = None) -> Plan:
    """要求1つを読み、送る前に確かめる ID を並べる。分からなければ Refused。"""
    method = method.upper()
    parts = [part for part in path.split("?", 1)[0].strip("/").split("/") if part]
    operation, path_id, kind = _route(method, parts)
    targets: list[tuple[str, str]] = [(path_id, kind)] if path_id else []
    if operation == "POST /search":
        return Plan(operation, (), search=True)
    if operation == "GET /views":
        targets += [(value, _QUERY_KEYS[key]) for key, value in query if key in _QUERY_KEYS]
        if not targets:
            raise Refused("ビューの一覧には database_id か data_source_id が要ります")
        return Plan(operation, tuple(targets))
    if body is not None and not isinstance(body, dict):
        raise Refused("本文は JSON のオブジェクトにしてください")
    body = body or {}
    found = list(references(body, _MARKDOWN_KEYS.get(operation, frozenset())))
    if operation in ("POST /pages", "POST /databases", "POST /data_sources"):
        parent = body.get("parent")
        if not isinstance(parent, dict) or not any(isinstance(parent.get(key), str) for key in _ID_KEYS):
            raise Refused("作る場所（parent）がありません。ワークスペース直下には作れません")
    if operation == "POST /pages/{id}/move" and not isinstance(body.get("parent"), dict):
        raise Refused("移す先（parent）がありません")
    if operation == "POST /views" and not found:
        raise Refused("ビューを置く場所（database_id など）がありません")
    moved = (path_id,) if path_id and (operation.endswith("/move") or "parent" in body) else ()
    return Plan(operation, tuple(targets + found), moved=moved)


def references(value: object, markdown: frozenset[str] = frozenset()) -> Iterator[tuple[str, str]]:
    """本文の中の、Notion の ID への参照をすべて拾う。ワークスペース直下を指す parent は Refused。

    markdown に挙げたキーの文字列は、Notion の Markdown としてページ参照を探す。
    """
    if isinstance(value, Mapping):
        if value.get("type") == "workspace" or value.get("workspace") is True:
            raise Refused("ワークスペース直下は扱えません")
        for key, item in value.items():
            if key in _ID_KEYS and isinstance(item, str):
                yield item, _ID_KEYS[key]
            elif key == "relation" and isinstance(item, list):
                yield from ((ref["id"], "page") for ref in item
                            if isinstance(ref, Mapping) and isinstance(ref.get("id"), str))
            elif key == "mention" and isinstance(item, Mapping):
                for name, kind in _MENTIONS.items():
                    ref = item.get(name)
                    if isinstance(ref, Mapping) and isinstance(ref.get("id"), str):
                        yield ref["id"], kind
            elif key == "after_block" and isinstance(item, Mapping) and isinstance(item.get("id"), str):
                yield item["id"], "block"
            elif key in markdown and isinstance(item, str):
                yield from markdown_references(item)
            yield from references(item, markdown)
    elif isinstance(value, list):
        for item in value:
            yield from references(item, markdown)


def markdown_references(text: str) -> Iterator[tuple[str, str]]:
    """Notion の Markdown（`<page url="…">`、`<mention-page url="…">` など）が指す ID。"""
    for tag, url in _MARKDOWN_REF.findall(text):
        found = parse_id(url)
        if found:
            yield found, _MARKDOWN_TAGS[tag.lower()]

