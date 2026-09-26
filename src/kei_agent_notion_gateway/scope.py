"""Notion のどのホームの中かを確かめる。

Notion の連携は共有されたページすべてに届くので、ID を渡されたら、その祖先に client のホームが
あるかを自分で確かめるしかない。親をたどってホームに着けば中、着かなければ外。

分からないときは通さない。見つからない、ワークスペース直下、循環、深すぎは、どれも拒否する。
「たぶん中だろう」で通すと、ほかのホームを壊せてしまう。

データベースは、中身（データソース）もホームの中になければ通さない。ホームの中に置いた
リンクドデータベースが外のデータソースを見せていると、そこへの書き込みや移動は外に届くため。

親子関係は全 client で共有して60秒だけ覚える。ゲートウェイで動かしたものはすぐ忘れ、
Notion の画面で動かされたものも長くは古いままにしない。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from kei_agent.config import notion_id
from kei_agent.notion import NotionError

# Notion の ID（ハイフンの有無は問わない）。前後に16進の文字が続くものは ID ではない
_ID = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                 r"[0-9a-fA-F]{12}(?![0-9a-fA-F])")
# 種類ごとの API の置き場所
PATHS = {"page": "pages", "block": "blocks", "database": "databases", "data_source": "data_sources", "view": "views"}
# 種類が分からない ID を引く順番。ブロックの API はページとデータベースも引ける
_GUESSES = ("block", "data_source", "view")
# 親の書き方 → 親の種類
_PARENT_KINDS = {"page_id": "page", "block_id": "block", "database_id": "database", "data_source_id": "data_source"}
# 「この種類ではない・見つからない」と読んでよい Notion の返事。ほかの失敗（落ちているなど）は止める
_NOT_THIS = frozenset({400, 403, 404})
TTL_SECONDS = 60.0
MAX_ITEMS = 10_000
MAX_DEPTH = 100


class ScopeError(RuntimeError):
    """届かない ID。item_id は頼まれた ID（たどった先の祖先ではない）。"""

    def __init__(self, item_id: str, reason: str):
        super().__init__(reason)
        self.item_id = item_id


def parse_id(value: object) -> str:
    """ID か Notion の URL から、比べられる形の ID（ハイフンなし・小文字）を取り出す。読めなければ空文字。"""
    text = str(value or "").strip()
    if _ID.fullmatch(text):
        return notion_id(text)
    # URL（…/Title-<ID>?v=<ビュー> や …#<ブロック>）なら、パスの最後の ID
    found = _ID.findall(text.split("?", 1)[0].split("#", 1)[0])
    return notion_id(found[-1]) if found else ""


@dataclass(frozen=True)
class Node:
    kind: str
    # 親の種類と ID。ワークスペース直下など、たどれないときは空
    parent_kind: str
    parent_id: str


def node_of(item: dict, kind: str) -> Node:
    """Notion の返事から、種類と親を読む。"""
    kind = {"page": "page", "database": "database", "data_source": "data_source", "view": "view"}.get(
        str(item.get("object") or ""), kind)
    if kind == "block":
        # ブロックの API はページとデータベースも返す
        kind = {"child_page": "page", "child_database": "database"}.get(str(item.get("type") or ""), "block")
    parent = item.get("parent") or {}
    parent_type = str(parent.get("type") or "")
    parent_kind = _PARENT_KINDS.get(parent_type, "")
    parent_id = notion_id(str(parent.get(parent_type) or "")) if parent_kind else ""
    return Node(kind, parent_kind if parent_id else "", parent_id)


class Tree:
    """ID → 種類と親の覚え書き。"""

    def __init__(self, notion, ttl: float = TTL_SECONDS, max_items: int = MAX_ITEMS,
                 clock: Callable[[], float] = time.monotonic):
        self.notion = notion
        self.ttl = ttl
        self.max_items = max_items
        self.clock = clock
        self._nodes: dict[str, tuple[float, Node]] = {}
        self._sources: dict[str, tuple[float, tuple[str, ...]]] = {}
        # MCP の道具と中継の口は別々のスレッドで動く
        self._lock = threading.Lock()

    def node(self, item_id: str, kind: str | None = None) -> Node:
        """ID の種類と親。見つからなければ ScopeError、Notion が落ちていれば NotionError。"""
        with self._lock:
            cached = self._nodes.get(item_id)
        if cached and cached[0] > self.clock():
            return cached[1]
        for guess in dict.fromkeys([*([kind] if kind in PATHS else []), *_GUESSES]):
            try:
                item = self.notion.request("GET", f"/{PATHS[guess]}/{item_id}")
            except NotionError as e:
                if e.status in _NOT_THIS:
                    continue
                raise
            return self.remember(item_id, item, guess)
        raise ScopeError(item_id, "Notion で見つかりません（共有されていないか、ID が違います）")

    def sources(self, database_id: str) -> tuple[str, ...]:
        """データベースの中身（データソース）の ID。リンクドデータベースでは、ほかの場所のデータソース。"""
        with self._lock:
            cached = self._sources.get(database_id)
        if cached and cached[0] > self.clock():
            return cached[1]
        try:
            database = self.notion.request("GET", f"/databases/{database_id}")
        except NotionError as e:
            if e.status in _NOT_THIS:
                raise ScopeError(database_id, "データベースの中身を確かめられません") from None
            raise
        found = tuple(notion_id(str(source.get("id") or "")) for source in database.get("data_sources") or [])
        with self._lock:
            if len(self._sources) >= self.max_items:
                self._sources.clear()
            self._sources[database_id] = (self.clock() + self.ttl, found)
        return found

    def remember(self, item_id: str, item: dict, kind: str) -> Node:
        """手元にある Notion の返事（検索の結果など）から覚える。"""
        found = node_of(item, kind)
        with self._lock:
            if len(self._nodes) >= self.max_items:
                now = self.clock()
                self._nodes = {key: value for key, value in self._nodes.items() if value[0] > now}
                if len(self._nodes) >= self.max_items:
                    self._nodes.clear()
            self._nodes[notion_id(item_id)] = (self.clock() + self.ttl, found)
        return found

    def forget(self, item_id: str) -> None:
        """動かしたものの親を忘れる（次に聞かれたら Notion から読み直す）。"""
        with self._lock:
            self._nodes.pop(notion_id(item_id), None)
            self._sources.pop(notion_id(item_id), None)


class Scope:
    """1つの client が届くホームの中か。"""

    def __init__(self, client: str, roots: frozenset[str], tree: Tree, max_depth: int = MAX_DEPTH):
        self.client = client
        self.roots = roots
        self.tree = tree
        self.max_depth = max_depth

    def require(self, item_id: object, kind: str | None = None) -> str:
        """ホームの中なら、その ID の種類（page / block / database / data_source / view）を返す。"""
        start = parse_id(item_id)
        if not start:
            raise ScopeError(str(item_id), "Notion の ID として読めません")
        found = self._walk(start, kind)
        if found == "database":
            for source in self.tree.sources(start):
                try:
                    self._walk(source, "data_source")
                except ScopeError:
                    raise ScopeError(start, "ホームの外のデータソースを見せるデータベースです") from None
        return found

    def _walk(self, start: str, kind: str | None) -> str:
        """親をたどってホームに着けば、start の種類を返す。"""
        found = ""
        seen: set[str] = set()
        current, current_kind = start, kind
        for _ in range(self.max_depth):
            if current in self.roots:
                # ホームそのものはページ
                return found or "page"
            if current in seen:
                raise ScopeError(start, "Notion の親関係が循環しています")
            seen.add(current)
            try:
                node = self.tree.node(current, current_kind)
            except ScopeError as e:
                # 返すのは頼まれた ID だけ（たどった先の祖先の ID は外に出さない）
                raise ScopeError(start, str(e)) from None
            found = found or node.kind
            if not node.parent_id:
                raise ScopeError(start, "届くホームの外です")
            current, current_kind = node.parent_id, node.parent_kind
        raise ScopeError(start, "Notion の親階層が深すぎます")

    def allows(self, item: dict, kind: str) -> bool:
        """検索の結果1件がホームの中か。結果についてきた親を使い、分からなければ外として落とす。"""
        item_id = parse_id(item.get("id"))
        if not item_id:
            return False
        if item_id not in self.roots:
            self.tree.remember(item_id, item, kind)
        try:
            self.require(item_id, kind)
        except (ScopeError, NotionError):
            return False
        return True
