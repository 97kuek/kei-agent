"""共通 Notion ホームを作る・確かめる（`kei-agent-hub-setup`）。ふだんの読み書きは notion_hub.py の HubStore。

既定は読むだけで、足りないものを並べる。`--apply` のときだけ、足りない DB・列・表を作り、ID を hub.json に控える。
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from kei_agent.configuration.config import notion_id
from kei_agent.storage.notion import (
    Notion,
    NotionError,
    gateway_notion,
    write_json_atomic,
)
from kei_agent.storage.notion_hub import (
    CALENDAR_ADDITIONS,
    CALENDAR_REQUIRED,
    COLLECT_DEFAULT,
    COLLECT_TITLE,
    DAILY_PROPERTIES,
    LEARNING_PROPERTIES,
    LEARNING_TITLE,
    READING_PROPERTIES,
    READING_TITLE,
    TIME_CHART_NAME,
    TIME_PROPERTIES,
    TIME_TITLE,
    HubState,
)
from kei_agent.storage.notion_store import (
    markdown_to_blocks,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Source:
    database_id: str
    data_source_id: str
    properties: dict


def task_view_spec(due: str, done: str) -> dict:
    """共通ホームの「今週のタスク」の表。締切が今週・来週のものと、期限切れで終わっていないものを、締切の近い順に。"""
    return {
        "filter": {"and": [
            {"or": [{"property": due, "date": {when: {}}} for when in ("past_year", "this_week", "next_week")]},
            {"property": "状態", "status": {"does_not_equal": done}},
        ]},
        "sorts": [{"property": due, "direction": "ascending"}],
    }


def _without_property(value):
    """絞り込みと並べ替えから列の指し方（名前か ID か）を外す。見比べるときだけ使う。"""
    if isinstance(value, dict):
        return {key: _without_property(item) for key, item in value.items() if key not in ("property", "property_id")}
    if isinstance(value, list):
        return [_without_property(item) for item in value]
    return value


class HubSetup:
    """全接続を読取で確認してから不足分だけ作る。重複を推測しない。"""

    def __init__(self, notion: Notion, home_id: str, state_path: Path, *,
                 research_home_id: str,
                 course_home_id: str):
        self.notion = notion
        self.home_id = home_id
        self.state_path = state_path
        self.research_home_id = research_home_id
        self.course_home_id = course_home_id
        # 止めるほどではない失敗（グラフのビューなど）。呼び出し側が表示する
        self.warnings: list[str] = []

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
        if notion_id(db.get("parent", {}).get("page_id")) != notion_id(parent):
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
            if notion_id(saved.home_id) != notion_id(self.home_id):
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
                if not parent_id:
                    continue
                parent = self.notion.request("GET", f"/databases/{parent_id}")
                if notion_id(parent.get("parent", {}).get("page_id")) == notion_id(self.home_id):
                    matches.append(full)
            if len(matches) > 1:
                raise NotionError(f"{name} のリンクドビューが重複しています")
            if matches:
                existing_views[name] = matches[0]
            if saved_views.get(name) and (not matches or matches[0]["id"] != saved_views[name]):
                raise NotionError(f"{name} の保存済みリンクドビューを確認できません")
        return calendar, daily, tasks, assignments, existing_views

    def _owned_source(self, title: str, properties: dict) -> _Source | None:
        """共通ホームの直下の、Kei Agent が書く DB（時間記録・読みもの）。無ければ None。"""
        found = self._source(self.home_id, (title,), title, required={}, optional=True)
        if found:
            for name, spec in properties.items():
                actual = found.properties.get(name)
                if actual and actual.get("type") != next(iter(spec)):
                    raise NotionError(f"{title}の「{name}」の型が異なります")
        return found

    def _ensure_source(self, title: str, properties: dict, icon: str = "") -> _Source:
        """Kei Agent が書く DB を用意する。無ければ作り、足りない列を足す。"""
        source = self._owned_source(title, properties)
        if source is None:
            body = {"parent": {"type": "page_id", "page_id": self.home_id},
                    "title": [{"text": {"content": title}}],
                    "initial_data_source": {"properties": properties}}
            if icon:
                body["icon"] = {"type": "emoji", "emoji": icon}
            created = self.notion.request("POST", "/databases", body)
            source = self._owned_source(title, properties)
            if source is None or source.database_id != created["id"]:
                raise NotionError(f"作成した{title}を再確認できません")
        missing = {n: p for n, p in properties.items() if n not in source.properties}
        if missing:
            self.notion.request("PATCH", f"/data_sources/{source.data_source_id}", {"properties": missing})
            source = self._owned_source(title, properties)
            assert source is not None
        return source

    def _time_chart(self, source: _Source, saved_id: str) -> str:
        """週ごと・領域ごとの縦棒グラフ。作れなくても setup は止めない（warnings に残す）。"""
        try:
            if saved_id:
                with suppress(NotionError, KeyError):
                    if self.notion.request("GET", f"/views/{saved_id}").get("id") == saved_id:
                        return saved_id
            response = self.notion.request("GET", f"/views?database_id={source.database_id}")
            for view in response.get("results", []):
                full = self.notion.request("GET", f"/views/{view['id']}")
                if full.get("name") == TIME_CHART_NAME and full.get("type") == "chart":
                    return full["id"]
            props = source.properties
            created = self.notion.request("POST", "/views", {
                "database_id": source.database_id,
                "data_source_id": source.data_source_id,
                "name": TIME_CHART_NAME,
                "type": "chart",
                "configuration": {
                    "type": "chart",
                    "chart_type": "column",
                    "x_axis": {"type": "date", "property_id": props["開始"]["id"], "group_by": "week",
                               "sort": {"type": "ascending"}},
                    "y_axis": {"aggregator": "sum", "property_id": props["分"]["id"]},
                    "stack_by": {"type": "select", "property_id": props["領域"]["id"],
                                 "sort": {"type": "manual"}},
                },
            })
            return created["id"]
        except (NotionError, KeyError, TypeError) as e:
            message = f"時間記録のグラフを作れませんでした（Notion の画面で作ってください）: {e}"
            log.warning(message)
            self.warnings.append(message)
            return ""

    def inspect(self) -> list[str]:
        calendar, daily, tasks, assignments, views = self._preflight()
        time_source = self._owned_source(TIME_TITLE, TIME_PROPERTIES)
        reading = self._owned_source(READING_TITLE, READING_PROPERTIES)
        learning = self._owned_source(LEARNING_TITLE, LEARNING_PROPERTIES)
        return [
            f"今月の予定／予定カレンダー: {calendar.database_id}",
            f"日別記録: {daily.database_id if daily else '未作成'}",
            f"時間記録: {time_source.database_id if time_source else '未作成'}",
            f"読みもの: {reading.database_id if reading else '未作成（--apply で作る）'}",
            f"学びのノート: {learning.database_id if learning else '未作成（--apply で作る）'}",
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
            spec = task_view_spec(due, status)
            if name not in views:
                views[name] = self.notion.request("POST", "/views", {
                    "data_source_id": source.data_source_id,
                    "create_database": {"parent": {"type": "page_id", "page_id": self.home_id}},
                    "name": name,
                    "type": "table",
                    **spec,
                })
            elif any(_without_property(views[name].get(key)) != _without_property(value)
                     for key, value in spec.items()):
                # 前の絞り込み（締切が今週だけ）で作った表を、いまの絞り込みにそろえる
                self.notion.request("PATCH", f"/views/{views[name]['id']}", spec)
        if not any(block.get("type") == "child_page" and block["child_page"].get("title") == COLLECT_TITLE
                   for block in self.notion.children(self.home_id)):
            # 中身は利用者が直していくので、作るのは無いときだけ
            self.notion.request("POST", "/pages", {
                "parent": {"type": "page_id", "page_id": self.home_id},
                "icon": {"type": "emoji", "emoji": "🧺"},
                "properties": {"title": {"title": [{"text": {"content": COLLECT_TITLE}}]}},
                "children": markdown_to_blocks(COLLECT_DEFAULT),
            })
        time_source = self._ensure_source(TIME_TITLE, TIME_PROPERTIES)
        reading = self._ensure_source(READING_TITLE, READING_PROPERTIES, icon="📰")
        learning = self._ensure_source(LEARNING_TITLE, LEARNING_PROPERTIES, icon="📒")
        saved_chart = ""
        if self.state_path.exists():
            saved_chart = HubState(**json.loads(self.state_path.read_text(encoding="utf-8"))).time_chart_view_id
        chart_id = self._time_chart(time_source, saved_chart)
        state = HubState(self.home_id, calendar.data_source_id, daily.data_source_id,
                         calendar.database_id, daily.database_id,
                         tasks.data_source_id, assignments.data_source_id,
                         views["研究 Task"]["id"], views["授業課題"]["id"], daily_view_id,
                         time_source.database_id, time_source.data_source_id, chart_id,
                         reading_db_id=reading.database_id, reading_ds_id=reading.data_source_id,
                         learning_db_id=learning.database_id, learning_ds_id=learning.data_source_id)
        write_json_atomic(self.state_path, state.__dict__)
        return state


def main() -> None:
    """既定は読取専用。--apply だけ schema とリンクドビューを作る。"""
    import argparse
    import sys

    from kei_agent.configuration.config import load_config

    parser = argparse.ArgumentParser(prog="kei-agent-hub-setup")
    parser.add_argument("--apply", action="store_true", help="確認済みの親ページへ変更を適用する")
    args = parser.parse_args()
    config = load_config()
    try:
        notion = gateway_notion("kei-agent", config=config)
    except NotionError as error:
        parser.error(str(error))
    setup = HubSetup(notion, config.notion.hub_home, config.hub_state_path,
                     research_home_id=config.notion.research_home, course_home_id=config.notion.course_home)
    try:
        details = setup.inspect()
        print("\n".join(details))
        if args.apply:
            state = setup.run()
            print(f"適用完了: 日別記録 {state.daily_ds_id}、カレンダー {state.calendar_ds_id}、"
                  f"時間記録 {state.time_ds_id}、読みもの {state.reading_ds_id}、学びのノート {state.learning_ds_id}")
            for warning in setup.warnings:
                print(f"注意: {warning}")
        else:
            print("読取専用の確認です。変更するには --apply が必要です")
    except NotionError as error:
        sys.exit(f"共通ホームの設定を停止しました: {error}")
