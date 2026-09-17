"""Notion の「研究ホーム」を作る（docs/notion-layout.md）。フェーズ3で Ezra が読み書きするときの接続も兼ねる。

使い方:
    source ~/.config/zsh/local/research-assistant.zsh
    uv run ezra-notion-setup <研究ホームのページID>

何度実行しても、すでにあるデータベース・ビュー・見出しは作り直さない。作ったものの ID は
~/.local/state/ezra/notion.json に保存する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ezra.config import load_config

NOTION_VERSION = "2026-03-11"


class NotionError(RuntimeError):
    pass


class Notion:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            "https://api.notion.com/v1" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise NotionError(f"{method} {path}: {e.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise NotionError(f"{method} {path}: {e}") from None

    def children(self, block_id: str) -> list[dict]:
        results, cursor = [], None
        while True:
            query = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            resp = self.request("GET", f"/blocks/{block_id}/children{query}")
            results += resp["results"]
            if not resp.get("has_more"):
                return results
            cursor = resp["next_cursor"]


# レイアウトの定義

def _options(*pairs: tuple[str, str]) -> list[dict]:
    return [{"name": n, "color": c} for n, c in pairs]


THEMES = {
    "icon": "🗂",
    "description": "1テーマ = Slack の1チャンネル = ~/research/<名前>/。前提と検索キーワードは CLAUDE.md が正。",
    "properties": {
        "名前": {"title": {}},
        "状態": {"select": {"options": _options(("進行中", "green"), ("保留", "yellow"), ("完了", "gray"))}},
        "目的": {"rich_text": {}},
        "Slack": {"url": {}},
        "ディレクトリ": {"rich_text": {}},
        "最終更新": {"last_edited_time": {}},
    },
}

TASKS = {
    "icon": "✅",
    "description": "担当が Ezra で状態が「今夜やる」の Task は、フェーズ3から Ezra が 01:30 に実行する。",
    "properties": {
        "タイトル": {"title": {}},
        "状態": {"status": {"options": [
            {"name": "未着手", "color": "gray", "group": "To-do"},
            {"name": "今夜やる", "color": "purple", "group": "To-do"},
            {"name": "実行中", "color": "blue", "group": "In progress"},
            {"name": "確認待ち", "color": "orange", "group": "In progress"},
            {"name": "完了", "color": "green", "group": "Complete"},
        ]}},
        "担当": {"select": {"options": _options(("自分", "blue"), ("Ezra", "green"))}},
        "優先度": {"select": {"options": _options(("P0", "red"), ("P1", "orange"), ("P2", "gray"))}},
        "期日": {"date": {}},
        "Slack": {"url": {}},
        "結果": {"rich_text": {}},
        "作成日": {"created_time": {}},
    },
    "relations": {"テーマ": ("themes", "Task")},
}

NOTES = {
    "icon": "📝",
    "description": (
        "計画: 目的 / 仮説 / 条件 / 判断の基準 / Ezra への依頼。"
        "考察: 問い / 結果 / 解釈 / 次の一手。"
        "議論メモ: 相手 / 論点 / 決めたこと / 宿題。"
    ),
    "properties": {
        "タイトル": {"title": {}},
        "種類": {"select": {"options": _options(
            ("計画", "blue"), ("考察", "purple"), ("Daily", "yellow"), ("振り返り", "orange"), ("議論メモ", "gray"))}},
        "日付": {"date": {}},
        "書いた人": {"select": {"options": _options(("自分", "blue"), ("Ezra", "green"), ("Codex", "gray"))}},
        "Slack": {"url": {}},
        "ファイル": {"rich_text": {}},
    },
    "relations": {"テーマ": ("themes", "ノート")},
}

MILESTONES = {
    "icon": "🏁",
    "description": "学会の締切、中間発表など。",
    "properties": {
        "名前": {"title": {}},
        "期日": {"date": {}},
        "状態": {"select": {"options": _options(("予定", "gray"), ("準備中", "orange"), ("済み", "green"))}},
        "メモ": {"rich_text": {}},
    },
    "relations": {"テーマ": ("themes", "マイルストーン")},
}


# ノートのテンプレート。API ではテンプレートを作れないので、Notion の画面で空のテンプレートを作っておき、中身をここで書く
NOTE_TEMPLATES = [
    ("計画", "## 目的\n\n## 仮説\n\n## 条件（何を変えて何を測るか）\n\n## 判断の基準（どうなったら仮説を支持するか）\n\n"
             "## Ezra への依頼\n\nSlack に貼る依頼文。数分以上かかる処理はジョブにしてよい"),
    ("考察", "## 問い\n\n## 結果（図や Slack のスレッドへのリンク）\n\n## 解釈\n\n## 次の一手"),
    ("議論メモ", "## 相手（Codex、指導教員など）\n\n## 論点\n\n## 決めたこと\n\n## 宿題"),
]
_BLANK_TEMPLATE_NAMES = {"", "New page", "新規ページ", "Untitled", "無題"}


def _eq_select(prop: str, value: str) -> dict:
    return {"property": prop, "select": {"equals": value}}


def _eq_status(prop: str, value: str) -> dict:
    return {"property": prop, "status": {"equals": value}}


class Setup:
    def __init__(self, notion: Notion, home_page_id: str, state_path: Path):
        self.notion = notion
        self.home = home_page_id
        self.state_path = state_path
        self.state = json.loads(state_path.read_text()) if state_path.exists() else {}
        self.state["home_page_id"] = home_page_id
        self.log: list[str] = []

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    # ページとデータベース

    def child_page(self, parent: str, title: str, icon: str) -> str:
        for block in self.notion.children(parent):
            if block["type"] == "child_page" and block["child_page"]["title"] == title:
                return block["id"]
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": parent},
            "icon": {"type": "emoji", "emoji": icon},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
        })
        self.log.append(f"ページを作成: {title}")
        return page["id"]

    def database(self, key: str, parent: str, title: str, spec: dict) -> dict:
        """(database_id, data_source_id, property_ids) を返す。同名のデータベースがあれば使う。"""
        db_id = None
        for block in self.notion.children(parent):
            if block["type"] == "child_database" and block["child_database"]["title"] == title:
                db_id = block["id"]
        properties = dict(spec["properties"])
        if db_id is None:
            for name, (target, synced) in spec.get("relations", {}).items():
                properties[name] = {"relation": {
                    "data_source_id": self.state["databases"][target]["data_source_id"],
                    "type": "dual_property",
                    "dual_property": {"synced_property_name": synced},
                }}
            db = self.notion.request("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"text": {"content": title}}],
                "description": [{"text": {"content": spec["description"]}}],
                "icon": {"type": "emoji", "emoji": spec["icon"]},
                "initial_data_source": {"properties": properties},
            })
            db_id = db["id"]
            self.log.append(f"データベースを作成: {title}")
        db = self.notion.request("GET", f"/databases/{db_id}")
        ds_id = db["data_sources"][0]["id"]
        ds = self.notion.request("GET", f"/data_sources/{ds_id}")
        # 途中で作ったデータベースに、足りないプロパティとリレーションを足す
        missing = {n: p for n, p in spec["properties"].items() if n not in ds["properties"]}
        for name, (target, synced) in spec.get("relations", {}).items():
            if name not in ds["properties"]:
                missing[name] = {"relation": {
                    "data_source_id": self.state["databases"][target]["data_source_id"],
                    "type": "dual_property",
                    "dual_property": {"synced_property_name": synced},
                }}
        if missing:
            ds = self.notion.request("PATCH", f"/data_sources/{ds_id}", {"properties": missing})
            self.log.append(f"プロパティを追加: {title} {list(missing)}")
        info = {
            "database_id": db_id,
            "data_source_id": ds_id,
            "properties": {n: p["id"] for n, p in ds["properties"].items()},
            "url": db.get("url"),
        }
        self.state.setdefault("databases", {})[key] = info
        self.save()
        return info

    # ビュー

    def view_names(self, db: dict) -> set[str]:
        resp = self.notion.request("GET", f"/views?database_id={db['database_id']}")
        names = set()
        for v in resp.get("results", []):
            full = self.notion.request("GET", f"/views/{v['id']}")
            names.add(full.get("name"))
        return names

    def views(self, db: dict, specs: list[dict]) -> None:
        existing = self.view_names(db)
        for spec in specs:
            if spec["name"] in existing:
                continue
            self.notion.request("POST", "/views", {
                "database_id": db["database_id"], "data_source_id": db["data_source_id"], **spec,
            })
            self.log.append(f"ビューを作成: {spec['name']}")

    # ホーム

    def home_sections(self, sections: list[tuple[str, dict, dict]]) -> None:
        """見出しと、その下のリンクドビューを並べる。同じ見出しがあれば飛ばす。"""
        headings = {
            "".join(t["plain_text"] for t in b["heading_2"]["rich_text"])
            for b in self.notion.children(self.home) if b["type"] == "heading_2"
        }
        for title, db, view in sections:
            if title in headings:
                continue
            resp = self.notion.request("PATCH", f"/blocks/{self.home}/children", {"children": [
                {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": title}}]}},
            ]})
            heading_id = resp["results"][-1]["id"]
            self.notion.request("POST", "/views", {
                "data_source_id": db["data_source_id"],
                "create_database": {
                    "parent": {"type": "page_id", "page_id": self.home},
                    "position": {"type": "after_block", "block_id": heading_id},
                },
                **view,
            })
            self.log.append(f"ホームに追加: {title}")

    def note_templates(self) -> None:
        """ノートのテンプレートに名前・種類・本文を書く。空のテンプレートが足りなければ知らせる。"""
        from ezra.notion_store import markdown_to_blocks

        notes = self.state["databases"]["notes"]
        resp = self.notion.request("GET", f"/data_sources/{notes['data_source_id']}/templates")
        templates = resp.get("templates", [])
        names = {t["name"] for t in templates}
        blanks = [t for t in templates if t["name"] in _BLANK_TEMPLATE_NAMES]
        for kind, body in NOTE_TEMPLATES:
            if kind in names:
                continue
            if not blanks:
                self.log.append(f"テンプレート「{kind}」の空の枠がありません。Notion のノートで「新規テンプレート」を作ってから、もう一度実行してください")
                continue
            template = blanks.pop(0)
            self.notion.request("PATCH", f"/pages/{template['id']}", {"properties": {
                "タイトル": {"title": [{"type": "text", "text": {"content": kind}}]},
                "種類": {"select": {"name": kind}},
                "書いた人": {"select": {"name": "自分"}},
            }})
            self.notion.request("PATCH", f"/blocks/{template['id']}/children", {"children": markdown_to_blocks(body)})
            self.log.append(f"テンプレートを作成: {kind}")

    def run(self) -> None:
        self.notion.request("PATCH", f"/pages/{self.home}", {"icon": {"type": "emoji", "emoji": "🔬"}})
        themes = self.database("themes", self.home, "テーマ", THEMES)
        tasks = self.database("tasks", self.home, "Task", TASKS)
        notes = self.database("notes", self.home, "ノート", NOTES)
        strategy = self.child_page(self.home, "中長期の方針", "🧭")
        self.state["strategy_page_id"] = strategy
        milestones = self.database("milestones", strategy, "マイルストーン", MILESTONES)
        self.save()

        p = lambda db, name: db["properties"][name]  # noqa: E731
        self.views(themes, [
            {"name": "進行中", "type": "list", "filter": _eq_select("状態", "進行中")},
        ])
        self.views(tasks, [
            {"name": "ボード", "type": "board", "configuration": {"type": "board", "group_by": {
                "type": "status", "property_id": p(tasks, "状態"), "group_by": "option", "sort": {"type": "manual"}}}},
            {"name": "今夜", "type": "table",
             "filter": {"and": [_eq_select("担当", "Ezra"), _eq_status("状態", "今夜やる")]}},
            {"name": "確認待ち", "type": "table", "filter": _eq_status("状態", "確認待ち")},
            {"name": "今週の自分", "type": "table",
             "filter": {"and": [_eq_select("担当", "自分"), {"property": "期日", "date": {"this_week": {}}}]}},
        ])
        self.views(notes, [
            {"name": "最近", "type": "table", "sorts": [{"property": "日付", "direction": "descending"}]},
            {"name": "種類ごと", "type": "board", "configuration": {"type": "board", "group_by": {
                "type": "select", "property_id": p(notes, "種類"), "sort": {"type": "manual"}}}},
            {"name": "Daily と振り返り", "type": "calendar",
             "filter": {"or": [_eq_select("種類", "Daily"), _eq_select("種類", "振り返り")]},
             "configuration": {"type": "calendar", "date_property_id": p(notes, "日付")}},
        ])
        self.views(milestones, [
            {"name": "タイムライン", "type": "timeline",
             "configuration": {"type": "timeline", "date_property_id": p(milestones, "期日")}},
        ])

        self.home_sections([
            ("自分の Task", tasks, {
                "name": "自分の Task", "type": "table",
                "filter": {"and": [_eq_select("担当", "自分"),
                                   {"property": "状態", "status": {"does_not_equal": "完了"}}]},
                "sorts": [{"property": "期日", "direction": "ascending"}],
            }),
            ("最近の Daily と振り返り", notes, {
                "name": "最近の Daily と振り返り", "type": "list",
                "filter": {"or": [_eq_select("種類", "Daily"), _eq_select("種類", "振り返り")]},
                "sorts": [{"property": "日付", "direction": "descending"}],
            }),
            ("進行中のテーマ", themes, {
                "name": "進行中のテーマ", "type": "list", "filter": _eq_select("状態", "進行中"),
            }),
            ("近いマイルストーン", milestones, {
                "name": "近いマイルストーン", "type": "list",
                "filter": {"property": "期日", "date": {"on_or_after": "today"}},
                "sorts": [{"property": "期日", "direction": "ascending"}],
            }),
        ])
        self.note_templates()
        self.save()

    def add_theme(self, name: str, purpose: str, slack_url: str, directory: str) -> None:
        themes = self.state["databases"]["themes"]
        found = self.notion.request("POST", f"/data_sources/{themes['data_source_id']}/query", {
            "filter": {"property": "名前", "title": {"equals": name}},
        })
        if found["results"]:
            return
        self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": themes["data_source_id"]},
            "properties": {
                "名前": {"title": [{"text": {"content": name}}]},
                "状態": {"select": {"name": "進行中"}},
                "目的": {"rich_text": [{"text": {"content": purpose}}]},
                "Slack": {"url": slack_url},
                "ディレクトリ": {"rich_text": [{"text": {"content": directory}}]},
            },
        })
        self.log.append(f"テーマを登録: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ezra-notion-setup")
    parser.add_argument("home_page_id", help="研究ホームのページID（URL の末尾32文字）")
    args = parser.parse_args()
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN が設定されていません（deploy/README.md を参照）")
    config = load_config()
    setup = Setup(Notion(token), args.home_page_id, config.state_dir / "notion.json")
    try:
        setup.run()
    finally:
        print("\n".join(setup.log) or "変更なし")
    print(f"状態: {setup.state_path}")


if __name__ == "__main__":
    main()
