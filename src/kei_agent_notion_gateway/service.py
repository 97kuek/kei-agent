"""研究ホームの中だけを触れる、型のある Notion 操作。

MCP の tool は、ここにある関数としか対応させない。任意の method と path を外から受け取る関数は
作らない（それを1つ用意すると、研究ホームの外へも手が届いてしまう）。

どの操作も、Notion を呼ぶ前に対象と親を `ResearchScope` に通す。移動と複製は、元と先の両方を通す。
記録に残すのは、操作名・対象 ID・成功したか・失敗の種類だけ。本文、プロパティの値、
検索結果、Authorization は残さない。
"""

from __future__ import annotations

from logging import Logger
from typing import Any

from kei_agent.notion import Notion, NotionError
from kei_agent_notion_gateway.scope import ResearchScope, ScopeError

# 1回の検索で返す件数の上限（研究ホームの外を落としたあとの数）
MAX_RESULTS = 50


class ResearchNotion:
    def __init__(self, notion: Notion, scope: ResearchScope, audit: Logger):
        self.notion = notion
        self.scope = scope
        self.audit = audit
        self._time_logs: str | None = None
        self._recorded_time_ids: set[str] = set()

    # 読む

    def read(self, target_id: str) -> dict:
        """ページ・ブロック・データベースの中身と、その直下の子ブロック。"""
        self._begin(target_id)
        return self._audited("read", target_id, lambda: {
            "item": self.scope.fetch(target_id), "children": self._children(target_id)})

    def _children(self, target_id: str) -> list[dict]:
        """直下の子ブロック。子を持てないもの（データソースなど）では空にする。"""
        try:
            return self.notion.children(target_id)
        except NotionError:
            return []

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        """研究ホームの中だけを検索する。

        Notion の検索はアカウント全体に届くので、返ってきたものを1つずつ研究ホームの子孫か
        確かめ、外のものは落とす。落とした件数も記録しない（何があるかが分かってしまう）。
        """
        self._begin(self.scope.root_id)
        found = self._call("search", "POST", "/search", {"query": query}, self.scope.root_id)
        inside = []
        for item in found.get("results") or []:
            try:
                self.scope.require(str(item.get("id") or ""))
            except ScopeError:
                continue
            inside.append(item)
            if len(inside) >= limit:
                break
        return inside

    def query(self, data_source_id: str, filter: dict | None = None,  # Notion の body のキーに合わせる
              sorts: list[dict] | None = None) -> list[dict]:
        """データソース（データベースの中身）を絞って読む。"""
        self._begin(data_source_id)
        body: dict[str, Any] = {"page_size": 100}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        found = self._call("query", "POST", f"/data_sources/{data_source_id}/query", body, data_source_id)
        return found.get("results") or []

    # 作る・直す

    def create_page(self, parent_id: str, properties: dict, children: list[dict] | None = None) -> dict:
        self._begin(parent_id)
        return self._call("create_page", "POST", "/pages", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": properties,
            "children": children or [],
        }, parent_id)

    def update_page(self, target_id: str, properties: dict) -> dict:
        self._begin(target_id)
        return self._call("update_page", "PATCH", f"/pages/{target_id}", {"properties": properties}, target_id)

    def append_blocks(self, target_id: str, children: list[dict]) -> dict:
        self._begin(target_id)
        return self._call("append_blocks", "PATCH", f"/blocks/{target_id}/children",
                          {"children": children}, target_id)

    def archive(self, target_id: str) -> dict:
        """ページをアーカイブする（Notion にはゴミ箱があるので、消すのはこれ）。"""
        self._begin(target_id)
        return self._call("archive", "PATCH", f"/pages/{target_id}", {"archived": True}, target_id)

    def move(self, source_id: str, destination_id: str) -> dict:
        self._begin(source_id, destination_id)
        return self._call("move", "PATCH", f"/pages/{source_id}", {
            "parent": {"type": "page_id", "page_id": destination_id},
        }, source_id)

    def duplicate(self, source_id: str, destination_id: str) -> dict:
        self._begin(source_id, destination_id)
        return self._call("duplicate", "POST", f"/pages/{source_id}/duplicate", {
            "parent": {"type": "page_id", "page_id": destination_id},
        }, source_id)

    def create_database(self, parent_id: str, title: str, properties: dict) -> dict:
        self._begin(parent_id)
        return self._call("create_database", "POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "title": [{"text": {"content": title}}],
            "initial_data_source": {"properties": properties},
        }, parent_id)

    def record_time(self, entry_id: str, started_at: str, duration_minutes: int, theme: str,
                    memo: str = "", slack_url: str = "") -> dict:
        """研究ホーム内の「研究ログ」に、同じ記録IDを一度だけ入れる。"""
        if not entry_id or not theme.strip() or not 1 <= duration_minutes <= 24 * 60:
            raise ValueError("研究時間の記録内容が不正です")
        data_source = self._research_time_logs()
        if entry_id in self._recorded_time_ids:
            return {"entry_id": entry_id, "notion_url": ""}
        found = self.query(data_source, {
            "property": "Kei Agent 記録ID", "rich_text": {"equals": entry_id},
        })
        if found:
            self._recorded_time_ids.add(entry_id)
            return {"entry_id": entry_id, "notion_url": found[0].get("url", "")}
        props = {
            "タイトル": {"title": [{"text": {"content": f"研究 / {theme.strip()}"}}]},
            "Kei Agent 記録ID": {"rich_text": [{"text": {"content": entry_id}}]},
            "日付": {"date": {"start": started_at}}, "時間（分）": {"number": duration_minutes},
            "テーマ": {"rich_text": [{"text": {"content": theme.strip()}}]},
            "メモ": {"rich_text": [{"text": {"content": memo[:1000]}}]} if memo else {"rich_text": []},
            "Slack": {"url": slack_url} if slack_url else {"url": None},
        }
        page = self._call("record_time", "POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": data_source}, "properties": props},
            self.scope.root_id)
        self._recorded_time_ids.add(entry_id)
        return {"entry_id": entry_id, "notion_url": page.get("url", "")}

    def _research_time_logs(self) -> str:
        """既存の研究ログを再利用し、なければ一度だけ作る。"""
        if self._time_logs:
            return self._time_logs
        # gateway が再起動しても同名DBを増やさない。検索結果も必ず scope で絞る。
        for item in self.search("研究ログ"):
            if item.get("object") != "database":
                continue
            database_id = str(item.get("id") or "")
            if not database_id:
                continue
            detail = self._call("read_time_logs", "GET", f"/databases/{database_id}", None,
                                self.scope.root_id)
            sources = detail.get("data_sources") or []
            source_id = str((sources[0] if sources else {}).get("id") or database_id)
            if source_id:
                self._time_logs = source_id
                return source_id
        db = self.create_database(self.scope.root_id, "研究ログ", {
                "タイトル": {"title": {}}, "Kei Agent 記録ID": {"rich_text": {}}, "日付": {"date": {}},
                "時間（分）": {"number": {"format": "number"}}, "テーマ": {"rich_text": {}},
                "メモ": {"rich_text": {}}, "Slack": {"url": {}},
            })
        sources = db.get("data_sources") or []
        self._time_logs = str((sources[0] if sources else {}).get("id") or db.get("id") or "")
        if not self._time_logs:
            raise NotionError("研究ログのデータソースIDを取得できませんでした")
        return self._time_logs

    def update_database(self, data_source_id: str, properties: dict) -> dict:
        """データベースの列（スキーマ）を足す・直す。"""
        self._begin(data_source_id)
        return self._call("update_database", "PATCH", f"/data_sources/{data_source_id}",
                          {"properties": properties}, data_source_id)

    # 中身

    def _begin(self, *item_ids: str) -> None:
        """1つの操作の始まり。親子関係の覚え書きを捨ててから、対象をすべて確かめる。

        覚え書きを持ち越すと、Notion 側でページを動かしたあとも、前の親のまま通してしまう。
        """
        self.scope.forget()
        for item_id in item_ids:
            self.scope.require(item_id)

    def _call(self, operation: str, method: str, path: str, body: dict | None, target_id: str) -> dict:
        return self._audited(operation, target_id, lambda: self.notion.request(method, path, body))

    def _audited(self, operation: str, target_id: str, action):
        """1回の操作を記録つきで行う。記録に残すのは操作名・対象 ID・成否・失敗の種類だけ。"""
        try:
            result = action()
        except (NotionError, ScopeError) as e:
            # 例外の本文には Notion が返した中身が入るので、種類だけを残す
            self.audit.warning("%s %s 失敗（%s）", operation, target_id, type(e).__name__, extra={
                "operation": operation, "target_id": target_id, "ok": False, "error_type": type(e).__name__})
            raise
        self.audit.info("%s %s", operation, target_id, extra={
            "operation": operation, "target_id": target_id, "ok": True, "error_type": ""})
        return result
