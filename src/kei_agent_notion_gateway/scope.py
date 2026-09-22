"""研究ホームの中かどうかを確かめる。

Notion の連携はアカウント全体に届くので、ページ ID を渡されたら、それが研究ホームの
子孫かを自分で確かめるしかない。親をたどって研究ホームに着けば中、着かなければ外。

分からないときは通さない。親が取れない、循環している、たどる回数が上限を超えた、のどれも拒否する。
「たぶん中だろう」で通すと、研究ホームの外を壊せてしまう。
"""

from __future__ import annotations

from kei_agent.notion import Notion, NotionError

# 親をたどる先の Notion API。ページ・ブロック・データソース・データベースの順に試す
_KINDS = ("pages", "blocks", "data_sources", "databases")
# 親として書かれうるキー
_PARENT_KEYS = ("page_id", "block_id", "database_id", "data_source_id")


class ScopeError(RuntimeError):
    pass


class ResearchScope:
    def __init__(self, notion: Notion, root_id: str, max_depth: int = 100):
        self.notion = notion
        self.root_id = root_id
        self.max_depth = max_depth
        self._items: dict[str, dict | None] = {}

    def require(self, item_id: str) -> None:
        """研究ホームの中でなければ ScopeError。"""
        seen: set[str] = set()
        current = item_id
        for _ in range(self.max_depth):
            if current == self.root_id:
                return
            if current in seen:
                raise ScopeError("Notion の親関係が循環しています")
            seen.add(current)
            current = self._parent_id(current)
            if not current:
                raise ScopeError("研究ホーム外の対象です")
        raise ScopeError("Notion の親階層が深すぎます")

    def forget(self) -> None:
        """覚えておいた親子関係を捨てる。1つの操作の中でだけ使い回す。"""
        self._items.clear()

    def fetch(self, item_id: str) -> dict:
        """ページ・ブロック・データベースのどれであっても、その中身を取る。"""
        item = self._item(item_id)
        if item is None:
            raise ScopeError("Notion で対象を見つけられませんでした")
        return item

    def _item(self, item_id: str) -> dict | None:
        if item_id in self._items:
            return self._items[item_id]
        found = None
        for kind in _KINDS:
            try:
                found = self.notion.request("GET", f"/{kind}/{item_id}")
                break
            except NotionError:
                continue
        self._items[item_id] = found
        return found

    def _parent_id(self, item_id: str) -> str | None:
        item = self._item(item_id)
        if item is None:
            return None
        parent = item.get("parent") or {}
        return next((parent[key] for key in _PARENT_KEYS if parent.get(key)), None)
