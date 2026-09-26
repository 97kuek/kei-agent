"""Notion の「研究ホーム」を作る（docs/architecture.md の「Notion」）。Kei Agent が読み書きするときの接続も兼ねる。

使い方（Notion ゲートウェイが動いていること）:
    source ~/.config/zsh/local/kei-agent.zsh
    uv run kei-agent-notion-setup [<研究ホームのページID>]

Notion を直接呼べるのはゲートウェイ（`kei-agent-notion-gateway`）だけ。ここからは
`gateway_notion()` でゲートウェイの `/notion/v1` を呼び、どのホームを触れるかはゲートウェイが決める。

何度実行しても、すでにあるデータベース・ビュー・見出しは作り直さない。作ったものの ID は
~/.local/state/kei-agent/notion.json に保存する。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kei_agent.config import Config, load_config, notion_id

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
# ゲートウェイの親の合言葉。これ自体は子プロセスに渡さず、名前ごとの合言葉を作るのにだけ使う
GATEWAY_TOKEN_ENV = "KEI_AGENT_NOTION_GATEWAY_TOKEN"
# ゲートウェイの利用者。どのホームに届くかはゲートウェイ側（kei_agent_notion_gateway.clients）が決める
GATEWAY_CLIENTS = ("kei-agent", "research", "course")
NO_GATEWAY = (f"{GATEWAY_TOKEN_ENV} がありません。Notion は Notion ゲートウェイ経由でだけ使えます"
              "（deploy/README.md の「秘密情報」）")
# Notion の上限はおよそ 3 リクエスト/秒。少し余裕をみて間隔をあける
MIN_INTERVAL_SECONDS = 0.34
# 429 や 5xx で待って試す回数
MAX_RETRIES = 3
# ページをたどる回数の上限（次のカーソルが返り続けても止まる）
MAX_PAGES = 200
# 1回のリクエストで足せるブロックの上限
BLOCKS_PER_REQUEST = 100


class NotionError(RuntimeError):
    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        # Notion（またはゲートウェイ）が返した HTTP の状態。つながらなかったときは None
        self.status = status


def gateway_client_token(master: str, client: str) -> str:
    """利用者ごとの合言葉（親の合言葉で client 名を HMAC-SHA256 したもの）。"""
    return hmac.new(master.encode(), client.encode(), hashlib.sha256).hexdigest()


def gateway_notion(client: str, env: dict[str, str] | None = None, config: Config | None = None) -> Notion:
    """ゲートウェイの `/notion/v1` を呼ぶ Notion。client の名前で届くホームが決まる。"""
    if client not in GATEWAY_CLIENTS:
        raise ValueError(f"未知のゲートウェイ利用者: {client}")
    env = dict(os.environ) if env is None else env
    master = env.get(GATEWAY_TOKEN_ENV, "").strip()
    if not master:
        raise NotionError(NO_GATEWAY)
    config = config or load_config()
    return Notion(gateway_client_token(master, client), base_url=config.notion_gateway_api)


class Notion:
    def __init__(self, token: str, base_url: str = NOTION_API):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._last_request = 0.0
        # ゲートウェイでは複数のスレッドから呼ぶので、間隔の計算を1つずつにする
        self._pace = threading.Lock()

    def _wait_turn(self) -> None:
        with self._pace:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def forward(self, method: str, path: str, data: bytes | None) -> tuple[int, bytes, dict[str, str]]:
        """1回だけ送り、Notion の返事（状態・本文・Retry-After）をそのまま返す。ゲートウェイ用。"""
        self._wait_turn()
        req = urllib.request.Request(self.base_url + path, method=method, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read(), {}
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except (OSError, http.client.HTTPException):
                body = b""
            headers = {"Retry-After": e.headers["Retry-After"]} if e.headers.get("Retry-After") else {}
            return e.code, body, headers

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Notion を1回呼ぶ。混んでいるとき（429）、一時的な失敗（5xx）、切断やタイムアウトは、待ってから試し直す。"""
        for attempt in range(MAX_RETRIES + 1):
            self._wait_turn()
            try:
                return self._send(method, path, body)
            except _Retryable as e:
                if attempt == MAX_RETRIES:
                    raise NotionError(f"{method} {path}: {e}", e.status) from None
                time.sleep(e.retry_after if e.retry_after is not None else 2 ** attempt)
        raise AssertionError("到達しない")

    def _send(self, method: str, path: str, body: dict | None) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                detail = ""
            if e.code == 429 or e.code >= 500:
                raise _Retryable(f"{e.code} {detail}", _retry_after(e), e.code) from None
            raise NotionError(f"{method} {path}: {e.code} {detail}", e.code) from None
        except json.JSONDecodeError as e:
            raise NotionError(f"{method} {path}: 応答を JSON として読めません: {e}") from None
        except (http.client.HTTPException, OSError) as e:
            # 切断・途中切れ・タイムアウト（URLError と TimeoutError も OSError）。Notion 側で書き込みが
            # 済んでいるかもしれないので、試し直すのは読むだけ・消すだけの要求に限る（二重にページを作らない）。
            # つながりもしなかった（ゲートウェイの再起動中など）なら何も届いていないので、書き込みでも試し直す
            refused = isinstance(getattr(e, "reason", e), ConnectionRefusedError)
            if refused or safe_to_resend(method, path):
                raise _Retryable(f"接続エラー: {e!r}{self._hint()}", None) from None
            raise NotionError(f"{method} {path}: 接続エラー（書き込みが済んだか分からない）: {e!r}{self._hint()}") from None

    def _hint(self) -> str:
        """ゲートウェイにつながらないときは、どこを見ればよいかを添える。"""
        return "" if self.base_url == NOTION_API else "（Notion ゲートウェイが動いているか確かめてください）"

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



def safe_to_resend(method: str, path: str) -> bool:
    """同じ要求をもう一度送っても結果が変わらないか。POST でも検索と DB の問い合わせは読むだけ。"""
    path = path.split("?", 1)[0].rstrip("/")
    return method in ("GET", "DELETE") or path.endswith(("/query", "/search")) or path == "/search"

def append_blocks(notion, block_id: str, blocks: list[dict], after: str | None = None) -> list[dict]:
    """ブロックを 100 件ずつ足す。`after` を渡すとそのブロックの直後に、順番を保って入れる。"""
    added: list[dict] = []
    for offset in range(0, len(blocks), BLOCKS_PER_REQUEST):
        body: dict = {"children": blocks[offset:offset + BLOCKS_PER_REQUEST]}
        if after:
            # 2026-03-11 から `after` は廃止され、position で指定する
            body["position"] = {"type": "after_block", "after_block": {"id": after}}
        results = notion.request("PATCH", f"/blocks/{block_id}/children", body).get("results") or []
        added += results
        if after and offset + BLOCKS_PER_REQUEST < len(blocks):
            if not results:
                raise NotionError("追加した block の ID を確認できません")
            after = results[-1]["id"]
    return added


class _Retryable(RuntimeError):
    def __init__(self, message: str, retry_after: float | None, status: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


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
    "description": "担当が Kei Agent で状態が「今夜やる」の Task は、Kei Agent が 00:00 に実行する。",
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


PAPERS = {
    "icon": "📚",
    "description": "先行研究。毎朝の新着（知識の担当が選ぶ）と、頼まれて調べた論文。ID で重ならないようにし、"
                   "同じ論文が複数のテーマに関係するときは、1行にテーマを並べる。",
    "properties": {
        "名前": {"title": {}},
        "URL": {"url": {}},
        "ID": {"rich_text": {}},
        "著者": {"rich_text": {}},
        "年": {"number": {"format": "number"}},
        "会場": {"rich_text": {}},
        "要点": {"rich_text": {}},
        "この研究との関係": {"rich_text": {}},
        "見つけた日": {"date": {}},
        "出どころ": {"select": {"options": _options(("毎朝の新着", "blue"), ("依頼", "green"))}},
        "状態": {"select": {"options": _options(("未読", "gray"), ("読んだ", "blue"), ("使う", "green"))}},
    },
    "relations": {"テーマ": ("themes", "先行研究")},
}
# テーマのページに置く、そのテーマの論文だけの表の名前
THEME_PAPERS_VIEW = "先行研究"


def theme_papers_view(papers: dict, theme_page_id: str) -> dict:
    """テーマのページに置く表（そのテーマの論文だけ、見つけた日の新しい順）。"""
    return {
        "database_id": papers["database_id"], "data_source_id": papers["data_source_id"],
        "create_database": {"parent": {"type": "page_id", "page_id": theme_page_id}},
        "name": THEME_PAPERS_VIEW, "type": "table",
        "filter": {"property": "テーマ", "relation": {"contains": theme_page_id}},
        "sorts": [{"property": "見つけた日", "direction": "descending"}],
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
SPECS = {"themes": THEMES, "tasks": TASKS, "notes": NOTES, "milestones": MILESTONES, "papers": PAPERS}


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


def write_json_atomic(path: Path, data) -> None:
    """一時ファイルに書いてから置き換える。途中で落ちても壊れた JSON を残さない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Setup:
    def __init__(self, notion: Notion, home_page_id: str, state_path: Path):
        self.notion = notion
        self.home = home_page_id
        self.state_path = state_path
        self.state = _read_state(state_path)
        self.state["home_page_id"] = home_page_id
        self.log: list[str] = []

    def save(self) -> None:
        write_json_atomic(self.state_path, self.state)

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
        matches = [block["id"] for block in self.notion.children(parent)
                   if block["type"] == "child_database" and block["child_database"]["title"] == title]
        if len(matches) > 1:
            raise NotionError(f"{title} という同名のデータベースが重複しています。正本を確認してから整理してください")
        db_id = matches[0] if matches else None
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

    def theme_paper_views(self, themes: dict, papers: dict) -> None:
        """どのテーマのページにも、そのテーマの論文だけの表を置く。もう置いてあるページは飛ばす。"""
        placed = set()
        response = self.notion.request("GET", f"/views?data_source_id={papers['data_source_id']}")
        for view in response.get("results", []):
            full = self.notion.request("GET", f"/views/{view['id']}")
            parent_db = (full.get("parent") or {}).get("database_id")
            if full.get("name") != THEME_PAPERS_VIEW or not parent_db or parent_db == papers["database_id"]:
                continue
            parent = self.notion.request("GET", f"/databases/{parent_db}")
            placed.add(notion_id((parent.get("parent") or {}).get("page_id")))
        rows = self.notion.paginate("POST", f"/data_sources/{themes['data_source_id']}/query", {"page_size": 100})
        for row in rows:
            if notion_id(row["id"]) not in placed:
                self.notion.request("POST", "/views", theme_papers_view(papers, row["id"]))
                self.log.append("テーマのページに先行研究の表を作成")

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
        papers = self.database("papers", self.home, "先行研究", PAPERS)
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
        self.views(papers, [
            {"name": "未読", "type": "table", "filter": _eq_select("状態", "未読"),
             "sorts": [{"property": "見つけた日", "direction": "descending"}]},
        ])
        self.theme_paper_views(themes, papers)

        self.home_sections([
            ("自分の Task", tasks, {
                "name": "自分の Task", "type": "table",
                "filter": {"and": [_eq_select("担当", "自分"),
                                   {"property": "状態", "status": {"does_not_equal": "完了"}}]},
                "sorts": [{"property": "期日", "direction": "ascending"}],
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
    parser.add_argument("home_page_id", nargs="?", default="",
                        help="研究ホームのページID（省くと config.toml の [notion] research_home）")
    args = parser.parse_args()
    config = load_config()
    try:
        notion = gateway_notion("kei-agent", config=config)
    except NotionError as e:
        sys.exit(str(e))
    home = args.home_page_id or config.notion.research_home
    if not home:
        sys.exit("研究ホームのページ ID がありません（config.toml の [notion] research_home）")
    setup = Setup(notion, home, config.state_dir / "notion.json")
    try:
        setup.run()
    finally:
        print("\n".join(setup.log) or "変更なし")
    print(f"状態: {setup.state_path}")


if __name__ == "__main__":
    main()
