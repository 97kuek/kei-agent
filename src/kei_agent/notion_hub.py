"""Kei Agent 本体だけが扱う共通 Notion ホーム。agent 用 gateway とは分離する。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from kei_agent.notion import BLOCKS_PER_REQUEST, Notion, NotionError, append_blocks, write_json_atomic
from kei_agent.notion_store import Note, blocks_to_markdown, markdown_to_blocks, plain_text, rich_text, summarize

log = logging.getLogger(__name__)

HUB_HOME_ID = "3e54fb5d2d07808dbe29fdbe67a4de56"
RESEARCH_HOME_ID = "3de4fb5d2d0780b9a193f4604d5ea09c"
COURSE_HOME_ID = "3e14fb5d2d07805ab7b2c01aab6b39a7"

DAILY_PROPERTIES = {
    "日付": {"title": {}},
    "Daily": {"rich_text": {}},
    "レトプラ": {"rich_text": {}},
    "対象日": {"date": {}},
    "Daily Slack": {"url": {}},
    "レトプラ Slack": {"url": {}},
    "Daily ファイル": {"rich_text": {}},
    "レトプラ ファイル": {"rich_text": {}},
    "移行元 ID": {"rich_text": {}},
}
CALENDAR_REQUIRED = {"名前": "title", "日付": "date", "タグ": "multi_select"}
CALENDAR_ADDITIONS = {
    "出典": {"select": {"options": [{"name": name} for name in ("Outlook", "課題", "手入力")]}},
    "出典 ID": {"rich_text": {}},
    "元 URL": {"url": {}},
    "最終確認": {"date": {}},
    "同期状態": {"select": {"options": [{"name": name} for name in ("確認済み", "要確認")]}},
    "場所": {"rich_text": {}},
    "元の状態": {"rich_text": {}},
}
MANAGED_END = "— Kei Agent の本文ここまで —"


@dataclass(frozen=True)
class HubState:
    home_id: str
    calendar_ds_id: str
    daily_ds_id: str
    calendar_db_id: str = ""
    daily_db_id: str = ""
    tasks_ds_id: str = ""
    assignments_ds_id: str = ""
    task_view_id: str = ""
    assignment_view_id: str = ""
    daily_view_id: str = ""


@dataclass(frozen=True)
class _Source:
    database_id: str
    data_source_id: str
    properties: dict


class HubSetup:
    """全接続を読取で確認してから不足分だけ作る。重複を推測しない。"""

    def __init__(self, notion: Notion, home_id: str, state_path: Path, *,
                 research_home_id: str = RESEARCH_HOME_ID,
                 course_home_id: str = COURSE_HOME_ID):
        self.notion = notion
        self.home_id = home_id
        self.state_path = state_path
        self.research_home_id = research_home_id
        self.course_home_id = course_home_id

    def _page(self, page_id: str, label: str) -> None:
        try:
            self.notion.request("GET", f"/pages/{page_id}")
        except (NotionError, KeyError):
            raise NotionError(f"{label} にアクセスできません。Kei Agent 本体の Notion 接続に共有してください") from None

    def _source(self, parent: str, names: tuple[str, ...], label: str, *,
                required: dict[str, str], optional: bool = False) -> _Source | None:
        matches = [b for b in self.notion.children(parent)
                   if b.get("type") == "child_database"
                   and b.get("child_database", {}).get("title") in names]
        if len(matches) > 1:
            raise NotionError(f"{label} の同名・旧名データベースが重複しています。正本を確認してください")
        if not matches:
            if optional:
                return None
            raise NotionError(f"{label} の正本データベースが見つかりません")
        db_id = matches[0]["id"]
        db = self.notion.request("GET", f"/databases/{db_id}")
        if db.get("parent", {}).get("page_id") != parent:
            raise NotionError(f"{label} の親ページが想定と異なります。正本を確認してください")
        sources = db.get("data_sources") or []
        if len(sources) != 1:
            raise NotionError(f"{label} に data source が {len(sources)} 件あります。正本を確認してください")
        ds_id = sources[0]["id"]
        properties = self.notion.request("GET", f"/data_sources/{ds_id}").get("properties", {})
        for name, kind in required.items():
            if properties.get(name, {}).get("type") != kind:
                raise NotionError(f"{label} の「{name}」は {kind} である必要があります")
        return _Source(db_id, ds_id, properties)

    def _daily_view(self, daily: _Source, saved_id: str = "") -> dict:
        response = self.notion.request("GET", f"/views?database_id={daily.database_id}")
        if response.get("has_more"):
            raise NotionError("日別記録のビューを全件確認できません")
        ids = [view["id"] for view in response.get("results", [])]
        if saved_id:
            if saved_id not in ids:
                raise NotionError("日別記録の保存済みビューが見つかりません")
            view_id = saved_id
        elif len(ids) == 1:
            view_id = ids[0]
        else:
            raise NotionError("日別記録の表示ビューが一意ではありません")
        view = self.notion.request("GET", f"/views/{view_id}")
        if view.get("type") != "table":
            raise NotionError("日別記録の表示ビューが表ではありません")
        return view

    def _preflight(self) -> tuple[_Source, _Source | None, _Source, _Source, dict[str, dict]]:
        self._page(self.home_id, "親ページ")
        self._page(self.research_home_id, "研究ホーム")
        self._page(self.course_home_id, "授業ホーム")
        calendar = self._source(self.home_id, ("今月の予定", "予定カレンダー"), "予定カレンダー",
                                required=CALENDAR_REQUIRED)
        daily = self._source(self.home_id, ("日別記録",), "日別記録", required={}, optional=True)
        if daily is not None and not self.state_path.exists():
            raise NotionError("既存の日別記録がありますが状態ファイルがありません。リンクドビューを手動確認してください")
        if daily:
            for name, spec in DAILY_PROPERTIES.items():
                actual = daily.properties.get(name)
                if actual and actual.get("type") != next(iter(spec)):
                    raise NotionError(f"日別記録の「{name}」の型が異なります")
        for name, spec in CALENDAR_ADDITIONS.items():
            actual = calendar.properties.get(name)
            if actual and actual.get("type") != next(iter(spec)):
                raise NotionError(f"予定カレンダーの「{name}」の型が異なります")
        tasks = self._source(self.research_home_id, ("Task",), "研究 Task",
                             required={"タイトル": "title", "期日": "date", "状態": "status"})
        assignments = self._source(self.course_home_id, ("課題",), "授業課題",
                                   required={"締切": "date", "状態": "status"})
        saved_views = {}
        saved_daily_id = ""
        if self.state_path.exists():
            try:
                saved = HubState(**json.loads(self.state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as e:
                raise NotionError(f"共通ホームの状態ファイルを確認できません: {e}") from None
            if saved.home_id != self.home_id:
                raise NotionError("共通ホームの状態ファイルの親ページが異なります")
            saved_views = {"研究 Task": saved.task_view_id, "授業課題": saved.assignment_view_id}
            saved_daily_id = saved.daily_view_id
        if daily is not None:
            self._daily_view(daily, saved_daily_id)
        existing_views: dict[str, dict] = {}
        for name, source in (("研究 Task", tasks), ("授業課題", assignments)):
            response = self.notion.request("GET", f"/views?data_source_id={source.data_source_id}")
            if response.get("has_more"):
                raise NotionError(f"{name} のビューを全件取得できません")
            matches = []
            for view in response.get("results", []):
                full = self.notion.request("GET", f"/views/{view['id']}")
                if full.get("name") != name:
                    continue
                parent_id = full.get("parent", {}).get("database_id")
                if parent_id:
                    parent = self.notion.request("GET", f"/databases/{parent_id}")
                    belongs_here = parent.get("parent", {}).get("page_id") == self.home_id
                else:
                    # テスト用 fake と旧応答のみ。実 API の view は parent.database_id を返す。
                    belongs_here = full.get("create_database", {}).get("parent", {}).get("page_id") == self.home_id
                if belongs_here:
                    matches.append(full)
            if len(matches) > 1:
                raise NotionError(f"{name} のリンクドビューが重複しています")
            if matches:
                existing_views[name] = matches[0]
            if saved_views.get(name) and (not matches or matches[0]["id"] != saved_views[name]):
                raise NotionError(f"{name} の保存済みリンクドビューを確認できません")
        return calendar, daily, tasks, assignments, existing_views

    def inspect(self) -> list[str]:
        calendar, daily, tasks, assignments, views = self._preflight()
        return [
            f"今月の予定／予定カレンダー: {calendar.database_id}",
            f"日別記録: {daily.database_id if daily else '未作成'}",
            f"研究 Task: {tasks.database_id}",
            f"授業課題: {assignments.database_id}",
            f"親ページのリンクドビュー: {', '.join(views) if views else '未作成'}",
        ]

    def run(self) -> HubState:
        calendar, daily, tasks, assignments, views = self._preflight()
        if daily is None:
            created = self.notion.request("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": self.home_id},
                "title": [{"text": {"content": "日別記録"}}],
                "initial_data_source": {"properties": DAILY_PROPERTIES},
            })
            daily = self._source(self.home_id, ("日別記録",), "日別記録", required={"日付": "title"})
            if daily is None or daily.database_id != created["id"]:
                raise NotionError("作成した日別記録を再確認できません")
        missing_daily = {n: p for n, p in DAILY_PROPERTIES.items() if n not in daily.properties}
        if missing_daily:
            self.notion.request("PATCH", f"/data_sources/{daily.data_source_id}", {"properties": missing_daily})
            daily = self._source(self.home_id, ("日別記録",), "日別記録", required={"日付": "title"})
        saved_daily_id = ""
        if self.state_path.exists():
            saved_daily_id = HubState(**json.loads(self.state_path.read_text(encoding="utf-8"))).daily_view_id
        daily_view = self._daily_view(daily, saved_daily_id)
        daily_view_id = daily_view["id"]
        shown_names = ("日付", "Daily", "レトプラ")
        shown_ids = [daily.properties[name]["id"] for name in shown_names]
        shown_actual = [prop["property_id"] for prop in
                        (daily_view.get("configuration") or {}).get("properties", []) if prop.get("visible")]
        if shown_actual != shown_ids:
            props = [{"property_id": daily.properties[name]["id"], "visible": name in shown_names}
                     for name in (*shown_names, *(name for name in daily.properties if name not in shown_names))]
            self.notion.request("PATCH", f"/views/{daily_view_id}", {
                "configuration": {"type": "table", "properties": props}})
        missing_calendar = {n: p for n, p in CALENDAR_ADDITIONS.items() if n not in calendar.properties}
        if missing_calendar:
            self.notion.request("PATCH", f"/data_sources/{calendar.data_source_id}", {"properties": missing_calendar})
        if any(b.get("child_database", {}).get("title") == "今月の予定" for b in self.notion.children(self.home_id)):
            self.notion.request("PATCH", f"/databases/{calendar.database_id}", {
                "title": [{"text": {"content": "予定カレンダー"}}]})
        for name, source, due, status in (
            ("研究 Task", tasks, "期日", "完了"),
            ("授業課題", assignments, "締切", "提出済み"),
        ):
            if name not in views:
                views[name] = self.notion.request("POST", "/views", {
                    "data_source_id": source.data_source_id,
                    "create_database": {"parent": {"type": "page_id", "page_id": self.home_id}},
                    "name": name,
                    "type": "table",
                    "filter": {"and": [
                        {"property": due, "date": {"this_week": {}}},
                        {"property": "状態", "status": {"does_not_equal": status}},
                    ]},
                })
        state = HubState(self.home_id, calendar.data_source_id, daily.data_source_id,
                         calendar.database_id, daily.database_id,
                         tasks.data_source_id, assignments.data_source_id,
                         views["研究 Task"]["id"], views["授業課題"]["id"], daily_view_id)
        write_json_atomic(self.state_path, state.__dict__)
        return state


class HubStore:
    def __init__(self, notion: Notion, state: HubState):
        self.notion = notion
        self.state = state

    def schema_problems(self) -> list[str]:
        """本体のハブだけ検査し、研究・授業 agent の接続範囲には入らない。"""
        self.notion.request("GET", f"/pages/{self.state.home_id}")
        problems = []
        for label, ds_id, required in (
            ("日別記録", self.state.daily_ds_id,
             {name: next(iter(spec)) for name, spec in DAILY_PROPERTIES.items()}),
            ("予定カレンダー", self.state.calendar_ds_id,
             {**CALENDAR_REQUIRED,
              **{name: next(iter(spec)) for name, spec in CALENDAR_ADDITIONS.items()}}),
        ):
            props = self.notion.request("GET", f"/data_sources/{ds_id}").get("properties", {})
            for name, kind in required.items():
                if props.get(name, {}).get("type") != kind:
                    problems.append(f"{label} の「{name}」が {kind} ではありません")
        return problems

    def _day_rows(self, day: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{self.state.daily_ds_id}/query", {
            "filter": {"property": "対象日", "date": {"equals": day}},
            "page_size": 100,
        })

    def _day(self, day: str) -> dict | None:
        rows = self._day_rows(day)
        if len(rows) > 1:
            raise NotionError(f"日別記録の {day} が重複しています。正本を確認してください")
        return rows[0] if rows else None

    @staticmethod
    def _section(blocks: list[dict], title: str) -> tuple[dict, list[dict]]:
        headings = [(i, block) for i, block in enumerate(blocks)
                    if block.get("type") == "heading_2"
                    and plain_text(block["heading_2"].get("rich_text", [])) == title]
        if len(headings) != 1:
            raise NotionError(f"日別記録の「{title}」区画がありません、または重複しています")
        index, heading = headings[0]
        end = next((i for i in range(index + 1, len(blocks))
                    if blocks[i].get("type") == "heading_2"), len(blocks))
        return heading, blocks[index + 1:end]

    @staticmethod
    def _managed_end(blocks: list[dict]) -> int:
        matches = [index for index, block in enumerate(blocks)
                   if block.get("type") == "paragraph"
                   and plain_text(block["paragraph"].get("rich_text", [])) == MANAGED_END]
        if len(matches) != 1:
            raise NotionError("日別記録の本文境界がありません、または重複しています。追記を守るため更新を止めます")
        return matches[0]

    def upsert_day(self, kind: Literal["Daily", "振り返り"], day: str, title: str,
                   markdown: str, slack_url: str | None, file: str | None) -> Note:
        if kind not in ("Daily", "振り返り"):
            raise ValueError(f"未知の記録種類: {kind}")
        date.fromisoformat(day)
        if not markdown.strip():
            raise ValueError("空の記録は保存できません")
        column = "Daily" if kind == "Daily" else "レトプラ"
        existing = self._day(day)
        old_blocks = self.notion.children(existing["id"]) if existing else []
        old_section: list[dict] = []
        heading = None
        if existing:
            heading, old_section = self._section(old_blocks, column)
            self._section(old_blocks, "レトプラ" if column == "Daily" else "Daily")
            managed_end = self._managed_end(old_section)
        summary = summarize(markdown, limit=200)
        properties = {
            column: {"rich_text": rich_text(summary)},
            f"{column} Slack": {"url": slack_url},
            f"{column} ファイル": {"rich_text": rich_text(file or "")},
        }
        content = markdown_to_blocks(markdown)
        if existing is None:
            properties.update({
                "日付": {"title": rich_text(day)},
                "対象日": {"date": {"start": day}},
                ("レトプラ" if column == "Daily" else "Daily"): {"rich_text": []},
            })
            sections = [
                markdown_to_blocks("## Daily"),
                content if column == "Daily" else [],
                markdown_to_blocks(MANAGED_END),
                markdown_to_blocks("## レトプラ"),
                content if column == "レトプラ" else [],
                markdown_to_blocks(MANAGED_END),
            ]
            blocks = [block for section in sections for block in section]
            page = self.notion.request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": self.state.daily_ds_id},
                "properties": properties,
                "children": blocks[:BLOCKS_PER_REQUEST],
            })
            append_blocks(self.notion, page["id"], blocks[BLOCKS_PER_REQUEST:])
        else:
            # 新しいブロックを先に置き、成功後に旧区画だけをゴミ箱へ移す。
            if content:
                append_blocks(self.notion, existing["id"], content, heading["id"])
            for block in old_section[:managed_end]:
                self.notion.request("PATCH", f"/blocks/{block['id']}", {"in_trash": True})
            page = self.notion.request("PATCH", f"/pages/{existing['id']}", {"properties": properties})
        return Note(page["id"], title, kind, day, page.get("url"), markdown)

    def day_body(self, day: str) -> str:
        page = self._day(day)
        if page is None:
            return ""
        return blocks_to_markdown(self.notion.children(page["id"]), self.notion.children)

    def set_legacy_ids(self, day: str, ids: list[str]) -> None:
        page = self._day(day)
        if page is None:
            raise NotionError(f"{day} の移行先が見つかりません")
        raw = plain_text(page.get("properties", {}).get("移行元 ID", {}).get("rich_text") or [])
        try:
            prior = json.loads(raw) if raw else []
        except ValueError:
            raise NotionError(f"{day} の移行元 ID を読めません") from None
        if not isinstance(prior, list) or not all(isinstance(item, str) for item in prior):
            raise NotionError(f"{day} の移行元 ID が不正です")
        merged = sorted(set(prior) | set(ids))
        if merged == sorted(prior):
            return
        payload = json.dumps(merged, ensure_ascii=False)
        if len(payload) > 2000:
            raise NotionError(f"{day} の移行元 ID が Notion の文字数上限を超えます")
        self.notion.request("PATCH", f"/pages/{page['id']}", {"properties": {
            "移行元 ID": {"rich_text": rich_text(payload)}}})

    def section_body(self, day: str, kind: Literal["Daily", "振り返り"]) -> str:
        """Kei Agent が書く本文（境界より前）だけ。境界と、その後の手書き・結論は含めない。"""
        page = self._day(day)
        if page is None:
            return ""
        blocks = self.notion.children(page["id"])
        _, content = self._section(blocks, "Daily" if kind == "Daily" else "レトプラ")
        return blocks_to_markdown(content[:self._managed_end(content)], self.notion.children)

    def append_review_conclusion(self, page_id: str, text: str, stamp: datetime,
                                 message_id: str | None = None) -> None:
        if not text.strip():
            return
        page = self.notion.request("GET", f"/pages/{page_id}")
        if page.get("parent", {}).get("data_source_id") != self.state.daily_ds_id:
            raise NotionError("振り返りの結論の保存先が日別記録ではありません")
        blocks = self.notion.children(page_id)
        heading, section = self._section(blocks, "レトプラ")
        self._managed_end(section)
        key = message_id or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        marker = f"Slack に貼った結論（{stamp:%m/%d %H:%M}、ID {key}）"
        if any(block.get("type") == "heading_3" and
               plain_text(block["heading_3"].get("rich_text", [])) == marker for block in section):
            return
        append_blocks(self.notion, page_id, markdown_to_blocks(f"### {marker}\n{text}"),
                      section[-1]["id"] if section else heading["id"])
        summary = plain_text(page["properties"]["レトプラ"].get("rich_text", []))
        self.notion.request("PATCH", f"/pages/{page_id}", {"properties": {
            "レトプラ": {"rich_text": rich_text(summarize(summary + "\n" + text, limit=200))}}})

    def reviews_edited_since(self, since: datetime) -> list[Note]:
        rows = self.notion.paginate("POST", f"/data_sources/{self.state.daily_ds_id}/query", {
            "filter": {"and": [
                {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since.astimezone().isoformat()}},
                {"property": "レトプラ", "rich_text": {"is_not_empty": True}},
            ]},
            "page_size": 100,
        })
        return [Note(row["id"], plain_text(row["properties"]["日付"]["title"]), "振り返り",
                     row["properties"]["対象日"]["date"]["start"], row.get("url"),
                     self.day_body(row["properties"]["対象日"]["date"]["start"])) for row in rows]

    def calendar_rows(self, source: str, window_start: date, window_end: date) -> list[dict]:
        """その出典の行のうち、日付が範囲内か空のもの。"""
        rows = self.notion.paginate("POST", f"/data_sources/{self.state.calendar_ds_id}/query", {
            "filter": {"and": [
                {"property": "出典", "select": {"equals": source}},
                {"or": [
                    {"property": "日付", "date": {"on_or_after": window_start.isoformat()}},
                    {"property": "日付", "date": {"is_empty": True}},
                ]},
            ]},
            "page_size": 100,
        })
        found = []
        for row in rows:
            day = (row["properties"].get("日付", {}).get("date") or {}).get("start") or ""
            # 入れ子の上限があるので、上端は取ってきてから絞る
            if day and day[:10] > window_end.isoformat():
                continue
            found.append({"id": row["id"],
                          "出典 ID": plain_text(row["properties"].get("出典 ID", {}).get("rich_text") or []),
                          "日付": day,
                          "同期状態": (row["properties"].get("同期状態", {}).get("select") or {}).get("name") or ""})
        return found

    def calendar_upsert(self, source: str, item, checked_at: datetime,
                        existing_id: str | None = None) -> None:
        properties = {
            "名前": {"title": rich_text(item.title)},
            "日付": {"date": {"start": item.start, **({"end": item.end} if item.end else {})}},
            "出典": {"select": {"name": source}},
            "出典 ID": {"rich_text": rich_text(item.source_id)},
            "元 URL": {"url": item.url or None},
            "最終確認": {"date": {"start": checked_at.astimezone().isoformat()}},
            "同期状態": {"select": {"name": "確認済み"}},
            "場所": {"rich_text": rich_text(item.location)},
            "元の状態": {"rich_text": rich_text(item.status)},
        }
        if existing_id:
            self.notion.request("PATCH", f"/pages/{existing_id}", {"properties": properties})
        else:
            self.notion.request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": self.state.calendar_ds_id},
                "properties": properties,
            })

    def calendar_mark_stale(self, row_id: str) -> None:
        self.notion.request("PATCH", f"/pages/{row_id}", {
            "properties": {"同期状態": {"select": {"name": "要確認"}}}})


def load_hub(config, env: dict[str, str] | None = None) -> HubStore | None:
    """設定済みの本体接続だけを使う。研究 Notion DB への代替保存はしない。"""
    env = dict(os.environ) if env is None else env
    token = env.get("NOTION_TOKEN")
    if not token or not config.hub_state_path.exists():
        log.warning("共通 Notion ホームが未設定です。日別記録への保存は行いません")
        return None
    try:
        raw = json.loads(config.hub_state_path.read_text(encoding="utf-8"))
        state = HubState(**raw)
    except (OSError, ValueError, TypeError) as e:
        log.error("共通 Notion ホームの状態ファイルを読めません: %s", e)
        return None
    if state.home_id != HUB_HOME_ID:
        log.error("共通 Notion ホームのページ ID が設定と一致しません")
        return None
    return HubStore(Notion(token), state)


def main() -> None:
    """既定は読取専用。--apply だけ schema とリンクドビューを作る。"""
    import argparse
    import sys

    from kei_agent.config import load_config

    parser = argparse.ArgumentParser(prog="kei-agent-hub-setup")
    parser.add_argument("--apply", action="store_true", help="確認済みの親ページへ変更を適用する")
    args = parser.parse_args()
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        parser.error("NOTION_TOKEN がありません")
    config = load_config()
    setup = HubSetup(Notion(token), HUB_HOME_ID, config.hub_state_path)
    try:
        details = setup.inspect()
        print("\n".join(details))
        if args.apply:
            state = setup.run()
            print(f"適用完了: 日別記録 {state.daily_ds_id}、カレンダー {state.calendar_ds_id}")
        else:
            print("読取専用の確認です。変更するには --apply が必要です")
    except NotionError as error:
        sys.exit(f"共通ホームの設定を停止しました: {error}")
