"""Notion の「研究ホーム」を作る（docs/notion-layout.md）。フェーズ3で Kei Agent が読み書きするときの接続も兼ねる。

使い方:
    source ~/.config/zsh/local/kei-agent.zsh
    uv run kei-agent-notion-setup <研究ホームのページID>

何度実行しても、すでにあるデータベース・ビュー・見出しは作り直さない。作ったものの ID は
~/.local/state/kei-agent/notion.json に保存する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kei_agent.config import load_config

NOTION_VERSION = "2026-03-11"
# Notion の上限はおよそ 3 リクエスト/秒。少し余裕をみて間隔をあける
MIN_INTERVAL_SECONDS = 0.34
# 429 や 5xx で待って試す回数
MAX_RETRIES = 3
# ページをたどる回数の上限（次のカーソルが返り続けても止まる）
MAX_PAGES = 200


class NotionError(RuntimeError):
    pass


class Notion:
    def __init__(self, token: str):
        self.token = token
        self._last_request = 0.0

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Notion を1回呼ぶ。混んでいるとき（429）と一時的な失敗（5xx）は、待ってから試し直す。"""
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                return self._send(method, path, body)
            except _Retryable as e:
                if attempt == MAX_RETRIES:
                    raise NotionError(f"{method} {path}: {e}") from None
                time.sleep(e.retry_after if e.retry_after is not None else 2 ** attempt)
        raise AssertionError("到達しない")

    def _send(self, method: str, path: str, body: dict | None) -> dict:
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
            if e.code == 429 or e.code >= 500:
                raise _Retryable(f"{e.code} {detail}", _retry_after(e)) from None
            raise NotionError(f"{method} {path}: {e.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise NotionError(f"{method} {path}: {e}") from None

    def paginate(self, method: str, path: str, body: dict | None = None) -> list[dict]:
        """`has_more` をたどって全部集める。GET はクエリ、POST は本文にカーソルを入れる。"""
        results: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            if method == "GET":
                query = {"page_size": 100} | ({"start_cursor": cursor} if cursor else {})
                resp = self.request("GET", f"{path}?{urllib.parse.urlencode(query)}")
            else:
                resp = self.request(method, path, (body or {}) | ({"start_cursor": cursor} if cursor else {}))
            results += resp.get("results") or []
            cursor = resp.get("next_cursor")
            # next_cursor が空のまま has_more が立つと、同じページを取り続けてしまう
            if not resp.get("has_more") or not cursor:
                return results
        raise NotionError(f"{method} {path}: ページが多すぎます（{MAX_PAGES} ページで打ち切り）")

    def children(self, block_id: str) -> list[dict]:
        return self.paginate("GET", f"/blocks/{block_id}/children")


class _Retryable(RuntimeError):
    def __init__(self, message: str, retry_after: float | None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    try:
        return float(error.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None


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
    "description": "担当が Kei Agent で状態が「今夜やる」の Task は、フェーズ3から Kei Agent が 01:30 に実行する。",
    "properties": {
        "タイトル": {"title": {}},
        "状態": {"status": {"options": [
            {"name": "未着手", "color": "gray", "group": "To-do"},
            {"name": "今夜やる", "color": "purple", "group": "To-do"},
            {"name": "実行中", "color": "blue", "group": "In progress"},
            {"name": "確認待ち", "color": "orange", "group": "In progress"},
            {"name": "完了", "color": "green", "group": "Complete"},
        ]}},
        "担当": {"select": {"options": _options(("自分", "blue"), ("Kei Agent", "green"))}},
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
        "計画: 目的 / 仮説 / 条件 / 判断の基準 / Kei Agent への依頼。"
        "考察: 問い / 結果 / 解釈 / 次の一手。"
        "議論メモ: 相手 / 論点 / 決めたこと / 宿題。"
    ),
    "properties": {
        "タイトル": {"title": {}},
        "種類": {"select": {"options": _options(
            ("計画", "blue"), ("考察", "purple"), ("Daily", "yellow"), ("振り返り", "orange"), ("議論メモ", "gray"))}},
        "日付": {"date": {}},
        "書いた人": {"select": {"options": _options(("自分", "blue"), ("Kei Agent", "green"), ("Codex", "gray"))}},
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
             "## Kei Agent への依頼\n\nSlack に貼る依頼文。数分以上かかる処理はジョブにしてよい"),
    ("考察", "## 問い\n\n## 結果（図や Slack のスレッドへのリンク）\n\n## 解釈\n\n## 次の一手"),
    ("議論メモ", "## 相手（Codex、指導教員など）\n\n## 論点\n\n## 決めたこと\n\n## 宿題"),
]
_BLANK_TEMPLATE_NAMES = {"", "New page", "新規ページ", "Untitled", "無題"}


# 状態のファイル（notion.json）のキーと、上の定義の対応。設定のずれを見つけるのに使う
SPECS = {"themes": THEMES, "tasks": TASKS, "notes": NOTES, "milestones": MILESTONES}


def _option_names(config: dict) -> set[str]:
    return {o["name"] for o in config.get("options", [])}


def schema_problems(notion: Notion, state: dict) -> list[str]:
    """Notion 側の項目が、Kei Agent が使う形からずれていないか見る。困るところを並べて返す。

    Notion の画面で選択肢の名前を変えると、絞り込みが 400 で落ちて、Daily や夜間の Task が黙って止まる。
    """
    problems = []
    for key, spec in SPECS.items():
        db = (state.get("databases") or {}).get(key)
        if db is None:
            problems.append(f"{key}: notion.json にありません（kei-agent-notion-setup を実行してください）")
            continue
        try:
            live = notion.request("GET", f"/data_sources/{db['data_source_id']}")["properties"]
        except NotionError as e:
            problems.append(f"{key}: 読めません（{e}）")
            continue
        for name, want in spec["properties"].items():
            kind = next(iter(want))
            have = live.get(name)
            if have is None:
                problems.append(f"{key}: 項目「{name}」がありません")
            elif have.get("type") != kind:
                problems.append(f"{key}: 項目「{name}」の種類が {have.get('type')} になっています（{kind} のはず）")
            elif kind in ("select", "status"):
                missing = _option_names(want[kind]) - _option_names(have.get(kind, {}))
                if missing:
                    problems.append(f"{key}: 項目「{name}」に選択肢 {'、'.join(sorted(missing))} がありません")
        for name in spec.get("relations", {}):
            if name not in live:
                problems.append(f"{key}: 項目「{name}」（リレーション）がありません")
    return problems


def _eq_select(prop: str, value: str) -> dict:
    return {"property": prop, "select": {"equals": value}}


def _eq_status(prop: str, value: str) -> dict:
    return {"property": prop, "status": {"equals": value}}


def _read_state(path: Path) -> dict:
    """作ったものの ID の控え。壊れていたら、作り直せるように空から始める。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise NotionError(f"{path} を読めません（消すと作り直します）: {e}") from None


class Setup:
    def __init__(self, notion: Notion, home_page_id: str, state_path: Path):
        self.notion = notion
        self.home = home_page_id
        self.state_path = state_path
        self.state = _read_state(state_path)
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
        from kei_agent.notion_store import markdown_to_blocks

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
             "filter": {"and": [_eq_select("担当", "Kei Agent"), _eq_status("状態", "今夜やる")]}},
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-notion-setup")
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
