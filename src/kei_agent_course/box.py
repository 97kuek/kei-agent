"""Box を読む（大学エージェント）。

学部要項や過去問は Box に置いたまま、必要なところだけ取り出して読む（ローカルに保存しない）。
アプリは「ユーザー認証（OAuth 2.0）」で、スコープは読み取りだけ。最初の1回だけブラウザで許可し、
以後はリフレッシュトークンで無人で更新する。Box のリフレッシュトークンは**使うたびに新しくなる**ので、
更新のたびに保存し直す（保存先は sandbox から読めない場所）。

    uv run --group course kei-agent-box-login          # 最初の1回。ブラウザで「許可」を押す
    uv run --group course kei-agent-box-login --probe  # 読めるかどうかを確かめる
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from kei_agent.config import load_config

log = logging.getLogger(__name__)

API = "https://api.box.com/2.0"
TOKEN_URL = "https://api.box.com/oauth2/token"
AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
APP_FILE_URL = "https://app.box.com/file/"
APP_FOLDER_URL = "https://app.box.com/folder/"
# 接続する先（sandbox の allowed_domains に入れるもの）
DOMAINS = ("api.box.com", "account.box.com", "public.boxcloud.com", "dl.boxcloud.com")

CLIENT_ID_ENV = "BOX_CLIENT_ID"
CLIENT_SECRET_ENV = "BOX_CLIENT_SECRET"
REDIRECT_ENV = "BOX_REDIRECT_URI"
DEFAULT_REDIRECT = "http://localhost:8799/box/callback"
NO_APP = (f"{CLIENT_ID_ENV} と {CLIENT_SECRET_ENV} がありません"
          "（Box の開発者コンソールでアプリを作り、大学用の秘密情報ファイルに入れてください）")
NO_TOKEN = "Box の許可がまだです（`kei-agent-box-login` を1回実行してください）"

TIMEOUT = 60
# アクセストークンは60分で切れる。この秒数を残して更新する
REFRESH_MARGIN = 300
SEARCH_LIMIT = 20
# 1ファイルから取り出すテキストの上限（長い要項をそのまま渡すと会話が重くなる）
TEXT_LIMIT = 60_000
# テキストが入っていない PDF から取り出すページ画像の枚数
MAX_PAGES = 3
# ページ画像の取り方。用意されているものはファイルごとに違うので、大きいほうから順に当たる
IMAGE_HINTS = ("jpg?dimensions=2048x2048", "jpg?dimensions=1024x1024", "jpg?dimensions=320x320")


class BoxError(RuntimeError):
    pass


@dataclass
class Tokens:
    refresh_token: str = ""
    access_token: str = ""
    expires_at: float = 0.0

    @property
    def fresh(self) -> bool:
        return bool(self.access_token) and self.expires_at - time.time() > REFRESH_MARGIN


@dataclass
class App:
    """Box のアプリ（開発者コンソールで作ったもの）。"""
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> App:
        env = dict(os.environ) if env is None else env
        client_id, client_secret = env.get(CLIENT_ID_ENV, ""), env.get(CLIENT_SECRET_ENV, "")
        if not client_id or not client_secret:
            raise BoxError(NO_APP)
        return cls(client_id, client_secret, env.get(REDIRECT_ENV) or DEFAULT_REDIRECT)


def token_path() -> Path:
    """リフレッシュトークンの置き場所（sandbox の denyRead に入れてある）。"""
    return Path(load_config().state_dir) / "secrets" / "box-token.json"


def read_tokens(path: Path | None = None) -> Tokens:
    path = path or token_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Tokens()
    return Tokens(refresh_token=data.get("refresh_token", ""),
                  access_token=data.get("access_token", ""),
                  expires_at=float(data.get("expires_at") or 0))


def write_tokens(tokens: Tokens, path: Path | None = None) -> None:
    path = path or token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "refresh_token": tokens.refresh_token,
        "access_token": tokens.access_token,
        "expires_at": tokens.expires_at,
    }, indent=2), encoding="utf-8")
    path.chmod(0o600)
    path.parent.chmod(0o700)


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise BoxError(f"Box のトークンを取れません（{e.code}）: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise BoxError(f"Box につながりません: {e}") from None


@dataclass
class Box:
    """Box を読むだけの薄い口。トークンの更新はここで面倒を見る。"""
    app: App
    tokens: Tokens = field(default_factory=Tokens)
    path: Path | None = None

    @classmethod
    def load(cls, env: dict[str, str] | None = None, path: Path | None = None) -> Box:
        path = path or token_path()
        tokens = read_tokens(path)
        if not tokens.refresh_token:
            raise BoxError(NO_TOKEN)
        return cls(App.from_env(env), tokens, path)

    def access_token(self) -> str:
        if self.tokens.fresh:
            return self.tokens.access_token
        got = _post(TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": self.tokens.refresh_token,
            "client_id": self.app.client_id,
            "client_secret": self.app.client_secret,
        })
        # Box はリフレッシュトークンも新しくして返す。保存し忘れると次回つながらない
        self.tokens = Tokens(refresh_token=got.get("refresh_token") or self.tokens.refresh_token,
                             access_token=got["access_token"],
                             expires_at=time.time() + float(got.get("expires_in") or 3600))
        write_tokens(self.tokens, self.path)
        log.info("Box のトークンを更新しました")
        return self.tokens.access_token

    # 読むための道具

    def _request(self, path: str, params: dict | None = None, headers: dict | None = None,
                 raw: bool = False, url: str | None = None) -> dict | bytes:
        target = url or f"{API}{path}"
        if params:
            target += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(target, headers={
            "Authorization": f"Bearer {self.access_token()}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read() if raw else json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise BoxError(f"Box の {path or target} が {e.code} を返しました: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise BoxError(f"Box につながりません: {e}") from None

    def search(self, query: str, limit: int = SEARCH_LIMIT, extensions: str = "") -> list[dict]:
        """名前と中身で探す。返すのは、そのまま人に見せられる短い形。"""
        params = {
            "query": query,
            "type": "file",
            "limit": max(1, min(limit, SEARCH_LIMIT)),
            "fields": "id,name,type,extension,size,modified_at,parent,path_collection",
        }
        if extensions:
            params["file_extensions"] = extensions
        found = self._request("/search", params)
        return [self._entry(e) for e in (found.get("entries") or [])]

    def folder_items(self, folder_id: str = "0", limit: int = 100) -> list[dict]:
        got = self._request(f"/folders/{folder_id}/items", {
            "limit": limit, "fields": "id,name,type,extension,size,modified_at"})
        return [self._entry(e) for e in (got.get("entries") or [])]

    def _entry(self, entry: dict) -> dict:
        kind = entry.get("type", "file")
        crumbs = [p.get("name") for p in ((entry.get("path_collection") or {}).get("entries") or [])
                  if p.get("name") and p.get("name") != "All Files"]
        return {
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "type": kind,
            "extension": entry.get("extension", ""),
            "size": entry.get("size"),
            "modified_at": entry.get("modified_at"),
            "folder": " / ".join(crumbs),
            "url": self.link(entry.get("id", ""), kind),
        }

    def link(self, file_id: str, kind: str = "file") -> str:
        """Box の画面で開く URL（共有リンクは作らない。読み取りだけの権限なので）。"""
        base = APP_FOLDER_URL if kind == "folder" else APP_FILE_URL
        return f"{base}{file_id}" if file_id else ""

    def _representation(self, file_id: str, hint: str) -> dict | None:
        got = self._request(f"/files/{file_id}", {"fields": "representations"},
                            headers={"X-Rep-Hints": f"[{hint}]"})
        entries = ((got.get("representations") or {}).get("entries") or [])
        return entries[0] if entries else None

    def text(self, file_id: str) -> str:
        """PDF や Office の本文をテキストで。入っていなければ空文字（スキャンや画像）。"""
        rep = self._representation(file_id, "extracted_text")
        if rep is None:
            return ""
        state = (rep.get("status") or {}).get("state")
        if state == "pending":
            raise BoxError("Box がまだ本文を用意しています（少し待ってから、もう一度）")
        if state != "success":
            return ""
        template = (rep.get("content") or {}).get("url_template") or ""
        if not template:
            return ""
        body = self._request("", url=template.replace("{+asset_path}", ""), raw=True)
        return body.decode("utf-8", "replace")[:TEXT_LIMIT]

    def page_images(self, file_id: str, pages: int = MAX_PAGES) -> list[bytes]:
        """ページを画像で取り出す（本文が入っていない PDF や、写真の過去問を claude に見せるため）。

        どの画像が用意されているかはファイルによって違うので、大きいほうから順に当たる。
        読み取りだけの権限ではファイルの本体はダウンロードできないが、この画像は取れる。
        """
        for hint in IMAGE_HINTS:
            rep = self._representation(file_id, hint)
            if rep is None or (rep.get("status") or {}).get("state") != "success":
                continue
            template = (rep.get("content") or {}).get("url_template") or ""
            if not template:
                continue
            total = (rep.get("metadata") or {}).get("pages")
            images = []
            for page in range(1, (min(pages, int(total)) if total else 1) + 1):
                asset = f"{page}.jpg" if total else ""
                try:
                    images.append(self._request("", url=template.replace("{+asset_path}", asset), raw=True))
                except BoxError as e:
                    log.warning("ページ画像を取れません（%s ページ目、%s）: %s", page, hint, e)
                    break
            if images:
                return images
        return []


# 最初の1回の許可（OAuth）


class _Callback(BaseHTTPRequestHandler):
    code: str = ""
    state: str = ""

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Callback.code = (query.get("code") or [""])[0]
        _Callback.state = (query.get("state") or [""])[0]
        ok = bool(_Callback.code)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = "Kei Agent（授業）が Box を読めるようになりました。ターミナルに戻ってください。" if ok \
            else "許可が取れませんでした。ターミナルの表示を見てください。"
        self.wfile.write(f"<html><body style='font-family:sans-serif'><p>{message}</p></body></html>".encode())

    def log_message(self, *args) -> None:
        return


def login(app: App | None = None, path: Path | None = None, open_browser: bool = True) -> Tokens:
    """ブラウザで1回だけ許可してもらい、リフレッシュトークンを保存する。"""
    app = app or App.from_env()
    state = secrets.token_urlsafe(16)
    url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": app.client_id,
        "redirect_uri": app.redirect_uri, "state": state})
    host, port = _host_port(app.redirect_uri)
    server = HTTPServer((host, port), _Callback)
    server.timeout = 300
    print(f"ブラウザで Box の許可画面を開きます。「許可」を押してください:\n{url}\n")
    if open_browser:
        os.system(f"open {url!r}")
    _Callback.code = _Callback.state = ""
    server.handle_request()
    server.server_close()
    if not _Callback.code:
        raise BoxError("許可が取れませんでした（画面で「許可」を押してから、もう一度）")
    if _Callback.state != state:
        raise BoxError("返ってきた state が違います（別の許可画面の結果かもしれません）")
    got = _post(TOKEN_URL, {
        "grant_type": "authorization_code", "code": _Callback.code,
        "client_id": app.client_id, "client_secret": app.client_secret,
        "redirect_uri": app.redirect_uri})
    tokens = Tokens(refresh_token=got["refresh_token"], access_token=got["access_token"],
                    expires_at=time.time() + float(got.get("expires_in") or 3600))
    write_tokens(tokens, path)
    return tokens


def _host_port(redirect_uri: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(redirect_uri)
    return (parsed.hostname or "localhost"), (parsed.port or 80)


def probe(box: Box) -> None:
    """読めるかどうかを確かめる（フォルダの一覧と、PDF・画像を1つずつ）。"""
    items = box.folder_items("0")
    print(f"一番上のフォルダ: {len(items)} 件")
    for item in items[:20]:
        print(f"  [{item['type']}] {item['name']}")
    for query, label in (("要項", "学部要項"), ("過去問", "過去問")):
        found = box.search(query, limit=5)
        print(f"\n「{query}」で検索: {len(found)} 件")
        for item in found:
            print(f"  {item['name']}（{item['folder'] or '-'}）{item['url']}")
        pdf = next((i for i in found if (i["extension"] or "").lower() == "pdf"), None)
        if pdf:
            text = box.text(pdf["id"])
            if text.strip():
                print(f"  → {pdf['name']} の本文 {len(text)} 文字: {re.sub(r'\\s+', ' ', text)[:120]}…")
            else:
                images = box.page_images(pdf["id"], pages=1)
                print(f"  → {pdf['name']} は本文が入っていない（ページ画像 {len(images)} 枚、"
                      f"{len(images[0]) if images else 0} バイト）")
        print(f"  （{label} の確認おわり）")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-box-login",
                                     description="Box を読む許可を1回だけ取る（読み取り専用）")
    parser.add_argument("--probe", action="store_true", help="許可済みのトークンで、読めるかどうかを確かめる")
    parser.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.probe:
            probe(Box.load())
            return
        login(open_browser=not args.no_open)
        print(f"許可を保存しました: {token_path()}")
        print("確かめるには: uv run --group course kei-agent-box-login --probe")
    except BoxError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
