"""Notion の「研究ホーム」を作る（docs/architecture.md の「Notion」）。Kei Agent が読み書きするときの接続も兼ねる。

使い方（Notion ゲートウェイが動いていること）:
    source ~/.config/kei-agent/secrets/kei-agent.zsh   # 置き場所は config.toml の [paths] secrets
    uv run kei-agent-notion-setup [<研究ホームのページID>]            作るもの・足すものを見るだけ
    uv run kei-agent-notion-setup --apply [<研究ホームのページID>]    書き込む

Notion を直接呼べるのはゲートウェイ（Notion のモジュール、`kei-agent-module notion`）だけ。ここからは
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

from kei_agent.configuration.config import MAIN_CLIENT, Config, load_config

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
# ゲートウェイの親の合言葉。これ自体は子プロセスに渡さず、名前ごとの合言葉を作るのにだけ使う
GATEWAY_TOKEN_ENV = "KEI_AGENT_NOTION_GATEWAY_TOKEN"
# 本体（と手で動かす setup）の利用者の名前。そのほかの利用者は agents.csv の notion 列に書いたホームの持ち主（gateway_clients）
KEI_AGENT = MAIN_CLIENT
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


def gateway_clients(config: Config) -> tuple[str, ...]:
    """ゲートウェイの利用者（本体と、agents.csv の notion 列にホームを書いたモジュール・研究）。"""
    return (KEI_AGENT, *config.notion.client_homes())


def gateway_notion(client: str, env: dict[str, str] | None = None, config: Config | None = None) -> Notion:
    """ゲートウェイの `/notion/v1` を呼ぶ Notion。client の名前（モジュールの名前）で届くホームが決まる。"""
    env = dict(os.environ) if env is None else env
    master = env.get(GATEWAY_TOKEN_ENV, "").strip()
    if not master:
        raise NotionError(NO_GATEWAY)
    config = config or load_config()
    if client not in gateway_clients(config):
        raise NotionError(f"{client} の Notion ホームが agents.csv の notion 列にありません"
                          f"（{client} の行の notion に、ページ ID を書いてください）")
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


def _status(*names_groups: tuple[str, str, str]) -> dict:
    return {"status": {"options": [{"name": n, "color": c, "group": g} for n, c, g in names_groups]}}


THEMES = {
    "icon": "🗂",
    "description": "1テーマ = Slack の1チャンネル = ~/research/<名前>/。前提とキーワード・進捗ログ・Task・先行研究は、テーマのページの中に置く。",
    "properties": {
        "Name": {"title": {}},
        "Status": _status(("In progress", "green", "In progress"), ("On hold", "yellow", "To-do"),
                          ("Done", "gray", "Complete")),
        "Start": {"date": {}},
        "End": {"date": {}},
        "Duration": {"formula": {"expression": (
            'if(empty(prop("Start")), "", if(empty(prop("End")), dateBetween(now(), prop("Start"), "days"), '
            'dateBetween(prop("End"), prop("Start"), "days")))')}},
    },
}

THEME_TASKS = {
    "icon": "✅",
    "description": "Owner が Kei で Status が Tonight の Task は、夜間の Task が実行する。Work & Result は「作業: …」「結果: …」で書く。",
    "properties": {
        "Title": {"title": {}},
        "Status": _status(("Not started", "gray", "To-do"), ("Tonight", "purple", "To-do"),
                          ("Running", "blue", "In progress"), ("Waiting", "orange", "In progress"),
                          ("Done", "green", "Complete")),
        "Owner": {"select": {"options": _options(("Me", "blue"), ("Kei", "green"))}},
        "Due": {"date": {}},
        "Work & Result": {"rich_text": {}},
        "Slack": {"url": {}},
    },
}

THEME_PAPERS = {
    "icon": "📚",
    "description": "このテーマの先行研究。Link で照合し、同じ論文を2行にしない（arXiv は abs の URL、バージョン番号は外す）。",
    "properties": {
        "Title": {"title": {}},
        "Status": {"select": {"options": _options(("Unread", "gray"), ("Read", "blue"), ("Use", "green"))}},
        "Summary": {"rich_text": {}},
        "Link": {"url": {}},
    },
}

TASKS_TITLE = "Task"
PAPERS_TITLE = "先行研究"
PREMISES_HEADING = "前提とキーワード"
LOG_HEADING = "進捗ログ"
STRATEGY_TITLE = "中長期の方針"
# 状態のファイル（notion.json）に控えるのは、テーマの DB だけ。Task と先行研究の DB はテーマのページの中にある
SPECS = {"themes": THEMES}
THEME_DATABASES = (("tasks", TASKS_TITLE, THEME_TASKS), ("papers", PAPERS_TITLE, THEME_PAPERS))


def theme_page_blocks() -> list[dict]:
    """テーマのページの先頭に置く見出しと、その下の空の段落。"""
    blocks = []
    for heading in (PREMISES_HEADING, LOG_HEADING):
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": heading}}]}})
        blocks.append({"type": "paragraph", "paragraph": {"rich_text": []}})
    return blocks


def _child_databases(notion, page_id: str, title: str) -> list[str]:
    return [b["id"] for b in notion.children(page_id)
            if b["type"] == "child_database" and b["child_database"]["title"] == title]


def create_theme_databases(notion, page_id: str) -> dict[str, str]:
    """テーマのページに Task と先行研究の DB を置く。あればそれを使う。{"tasks": ds, "papers": ds} を返す。"""
    found = {}
    for key, title, spec in THEME_DATABASES:
        ids = _child_databases(notion, page_id, title)
        if len(ids) > 1:
            raise NotionError(f"テーマのページ {page_id} に「{title}」の DB が {len(ids)} つあります。1つに整理してください")
        if ids:
            db_id = ids[0]
        else:
            db_id = notion.request("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": page_id},
                "is_inline": True,
                "title": [{"text": {"content": title}}],
                "description": [{"text": {"content": spec["description"]}}],
                "icon": {"type": "emoji", "emoji": spec["icon"]},
                "initial_data_source": {"properties": spec["properties"]},
            })["id"]
        found[key] = notion.request("GET", f"/databases/{db_id}")["data_sources"][0]["id"]
    return found


def _option_names(config: dict) -> set[str]:
    return {o["name"] for o in config.get("options", [])}


def _spec_problems(where: str, spec: dict, live: dict) -> list[str]:
    problems = []
    for name, want in spec["properties"].items():
        kind = next(iter(want))
        have = live.get(name)
        if have is None:
            problems.append(f"{where}: 項目「{name}」がありません")
        elif have.get("type") != kind:
            problems.append(f"{where}: 項目「{name}」の種類が {have.get('type')} になっています（{kind} のはず）")
        elif kind in ("select", "status"):
            missing = _option_names(want[kind]) - _option_names(have.get(kind, {}))
            if missing:
                problems.append(f"{where}: 項目「{name}」に選択肢 {'、'.join(sorted(missing))} がありません")
    return problems


def schema_problems(notion: Notion, state: dict) -> list[str]:
    """テーマの DB と、各テーマのページの Task・先行研究の列が、Kei Agent の使う形からずれていないか見る。

    Notion の画面で選択肢の名前を変えると、絞り込みが 400 で落ちて、Daily や夜間の Task が黙って止まる。
    """
    db = (state.get("databases") or {}).get("themes")
    if db is None:
        return ["themes: notion.json にありません（kei-agent-notion-setup --apply を実行してください）"]
    try:
        live = notion.request("GET", f"/data_sources/{db['data_source_id']}")["properties"]
    except NotionError as e:
        return [f"themes: 読めません（{e}）"]
    problems = _spec_problems("themes", THEMES, live)
    if problems:
        return problems
    rows = notion.paginate("POST", f"/data_sources/{db['data_source_id']}/query", {
        "page_size": 100, "filter": {"property": "Status", "status": {"equals": "In progress"}}})
    for row in rows:
        name = "".join(t.get("plain_text", "") for t in row["properties"]["Name"]["title"])
        try:
            children = notion.children(row["id"])
            for _, title, spec in THEME_DATABASES:
                ids = [b["id"] for b in children
                       if b["type"] == "child_database" and b["child_database"]["title"] == title]
                if len(ids) != 1:
                    problems.append(f"テーマ「{name}」: 「{title}」の DB が {len(ids)} つあります（1つのはず）")
                    continue
                ds = notion.request("GET", f"/databases/{ids[0]}")["data_sources"][0]["id"]
                problems += _spec_problems(f"テーマ「{name}」の{title}", spec,
                                           notion.request("GET", f"/data_sources/{ds}")["properties"])
        except NotionError as e:
            problems.append(f"テーマ「{name}」: ページを読めません（{e}）")
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

    def find_database(self, parent: str, title: str) -> str | None:
        """親ページの直下の、その名前のデータベース。無ければ None、2つ以上あれば止める。"""
        matches = [block["id"] for block in self.notion.children(parent)
                   if block["type"] == "child_database" and block["child_database"]["title"] == title]
        if len(matches) > 1:
            raise NotionError(f"{title} という同名のデータベースが重複しています。正本を確認してから整理してください")
        return matches[0] if matches else None

    def database(self, key: str, parent: str, title: str, spec: dict) -> dict:
        """(database_id, data_source_id, property_ids) を返す。同名のデータベースがあれば使う。"""
        db_id = self.find_database(parent, title)
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

    def home_headings(self) -> set[str]:
        return {"".join(t["plain_text"] for t in b["heading_2"]["rich_text"])
                for b in self.notion.children(self.home) if b["type"] == "heading_2"}

    def home_sections(self, sections: list[tuple[str, dict, dict]]) -> None:
        """見出しと、その下のリンクドビューを並べる。同じ見出しがあれば飛ばす。"""
        headings = self.home_headings()
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

    # 作るものの定義（run と plan で共通）

    def databases(self) -> list[tuple[str, str, str, dict]]:
        """（state の鍵、親ページ、名前、定義）を作る順に。"""
        return [("themes", self.home, "テーマ", THEMES)]

    @staticmethod
    def view_specs(dbs: dict[str, dict]) -> dict[str, list[dict]]:
        """データベースごとのビュー。"""
        return {"themes": [{"name": "In progress", "type": "list", "filter": _eq_status("Status", "In progress")}]}

    @staticmethod
    def section_specs() -> list[tuple[str, str, dict]]:
        """ホームの見出しと、その下に置くビュー（データベースは state の鍵で指す）。"""
        return [("進行中のテーマ", "themes", {
            "name": "進行中のテーマ", "type": "list", "filter": _eq_status("Status", "In progress")})]

    def run(self) -> None:
        self.notion.request("PATCH", f"/pages/{self.home}", {"icon": {"type": "emoji", "emoji": "🔬"}})
        self.state["strategy_page_id"] = self.child_page(self.home, STRATEGY_TITLE, "🧭")
        dbs = {key: self.database(key, parent, title, spec) for key, parent, title, spec in self.databases()}
        for key, specs in self.view_specs(dbs).items():
            self.views(dbs[key], specs)
        for row in self.notion.paginate("POST", f"/data_sources/{dbs['themes']['data_source_id']}/query",
                                        {"page_size": 100}):
            create_theme_databases(self.notion, row["id"])
        self.home_sections([(title, dbs[key], view) for title, key, view in self.section_specs()])
        self.save()

    def plan(self) -> list[str]:
        """--apply を付けないときの確認。読むだけで、作るもの・足すものを run と同じ順に並べる。"""
        lines: list[str] = []
        if not any(b["type"] == "child_page" and b["child_page"]["title"] == STRATEGY_TITLE
                   for b in self.notion.children(self.home)):
            lines.append(f"ページを作る: {STRATEGY_TITLE}")
        for _key, parent, title, spec in self.databases():
            db_id = self.find_database(parent, title)
            if db_id is None:
                lines.append(f"データベースを作る: {title}（ビューも作る）")
                continue
            ds_id = self.notion.request("GET", f"/databases/{db_id}")["data_sources"][0]["id"]
            ds = self.notion.request("GET", f"/data_sources/{ds_id}")
            if missing := [n for n in spec["properties"] if n not in ds["properties"]]:
                lines.append(f"列を足す: {title}（{'、'.join(missing)}）")
            for row in self.notion.paginate("POST", f"/data_sources/{ds_id}/query", {"page_size": 100}):
                absent = [t for _, t, _ in THEME_DATABASES if not _child_databases(self.notion, row["id"], t)]
                if absent:
                    lines.append(f"テーマのページに DB を置く: {row['id']}（{'、'.join(absent)}）")
        if missing := [t for t, _, _ in self.section_specs() if t not in self.home_headings()]:
            lines.append(f"ホームに見出しとビューを足す: {'、'.join(missing)}")
        return lines


def main() -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-notion-setup")
    parser.add_argument("home_page_id", nargs="?", default="",
                        help="研究ホームのページID（省くと agents.csv の research の行の notion）")
    parser.add_argument("--apply", action="store_true", help="書き込む（付けなければ、作るもの・足すものを見るだけ）")
    args = parser.parse_args()
    config = load_config()
    try:
        notion = gateway_notion("kei-agent", config=config)
    except NotionError as e:
        sys.exit(str(e))
    home = args.home_page_id or config.notion.research_home
    if not home:
        sys.exit("研究ホームのページ ID がありません（agents.csv の research の行の notion）")
    setup = Setup(notion, home, config.state_dir / "notion.json")
    if not args.apply:
        print("\n".join(setup.plan()) or "変更なし（そろっています）")
        print("読み取りだけの確認です。書き込むには --apply を付けてください")
        return
    try:
        setup.run()
    finally:
        print("\n".join(setup.log) or "変更なし")
    print(f"状態: {setup.state_path}")


if __name__ == "__main__":
    main()
