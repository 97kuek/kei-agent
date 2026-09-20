"""仕事の Microsoft 365（Outlook の予定）を読む。

会社のアカウントなので、読み取りだけの委任の権限（`Calendars.Read`）を使う。
最初の1回だけブラウザで許可し、以後はリフレッシュトークンで無人で更新する（Box と同じ形）。
アプリの秘密（client secret）は持たない公開クライアントで、PKCE を使う。

    uv run --group work kei-agent-ms-login          # 最初の1回。ブラウザで「承諾」を押す
    uv run --group work kei-agent-ms-login --probe  # 読めるかどうかを確かめる
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from kei_agent.config import load_config

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
# 会社のテナントを指定できるようにする（既定は、職場か学校のアカウント）
DEFAULT_TENANT = "organizations"
# 読むだけ。予定を書き換える権限は取らない
SCOPES = ("offline_access", "User.Read", "Calendars.Read")
DOMAINS = ("login.microsoftonline.com", "graph.microsoft.com")

CLIENT_ID_ENV = "MS_CLIENT_ID"
TENANT_ENV = "MS_TENANT_ID"
REDIRECT_ENV = "MS_REDIRECT_URI"
DEFAULT_REDIRECT = "http://localhost:8798/ms/callback"
NO_APP = (f"{CLIENT_ID_ENV} がありません"
          "（Entra ID でアプリを登録し、仕事用の秘密情報ファイルに入れてください）")
NO_TOKEN = "Microsoft の許可がまだです（`kei-agent-ms-login` を1回実行してください）"

TIMEOUT = 60
REFRESH_MARGIN = 300
# 1回に返す予定の数
MAX_EVENTS = 50
# 予定を見る先の長さ（日）
DEFAULT_DAYS = 7
# Graph に返してもらう時刻の帯（これを付けないと UTC で返る）
TIMEZONE = "Tokyo Standard Time"


class GraphError(RuntimeError):
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
    """Entra ID に登録したアプリ（公開クライアント。秘密は持たない）。"""
    client_id: str
    tenant: str = DEFAULT_TENANT
    redirect_uri: str = DEFAULT_REDIRECT

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> App:
        env = dict(os.environ) if env is None else env
        client_id = env.get(CLIENT_ID_ENV, "")
        if not client_id:
            raise GraphError(NO_APP)
        return cls(client_id, env.get(TENANT_ENV) or DEFAULT_TENANT,
                   env.get(REDIRECT_ENV) or DEFAULT_REDIRECT)

    @property
    def authorize_url(self) -> str:
        return f"{LOGIN}/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_url(self) -> str:
        return f"{LOGIN}/{self.tenant}/oauth2/v2.0/token"


def token_path() -> Path:
    """リフレッシュトークンの置き場所（sandbox の denyRead に入れてある）。"""
    return Path(load_config().state_dir) / "secrets" / "ms-token.json"


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
        detail = e.read().decode("utf-8", "replace")[:400]
        raise GraphError(f"Microsoft のトークンを取れません（{e.code}）: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise GraphError(f"Microsoft につながりません: {e}") from None


@dataclass
class Graph:
    """Outlook の予定を読むだけの薄い口。"""
    app: App
    tokens: Tokens = field(default_factory=Tokens)
    path: Path | None = None

    @classmethod
    def load(cls, env: dict[str, str] | None = None, path: Path | None = None) -> Graph:
        path = path or token_path()
        tokens = read_tokens(path)
        if not tokens.refresh_token:
            raise GraphError(NO_TOKEN)
        return cls(App.from_env(env), tokens, path)

    def access_token(self) -> str:
        if self.tokens.fresh:
            return self.tokens.access_token
        got = _post(self.app.token_url, {
            "grant_type": "refresh_token",
            "refresh_token": self.tokens.refresh_token,
            "client_id": self.app.client_id,
            "scope": " ".join(SCOPES),
        })
        self.tokens = Tokens(refresh_token=got.get("refresh_token") or self.tokens.refresh_token,
                             access_token=got["access_token"],
                             expires_at=time.time() + float(got.get("expires_in") or 3600))
        write_tokens(self.tokens, self.path)
        log.info("Microsoft のトークンを更新しました")
        return self.tokens.access_token

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{GRAPH}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.access_token()}",
            # これを付けないと、時刻が UTC で返ってくる
            "Prefer": f'outlook.timezone="{TIMEZONE}"',
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise GraphError(f"Microsoft Graph の {path} が {e.code} を返しました: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise GraphError(f"Microsoft につながりません: {e}") from None

    def me(self) -> dict:
        return self._get("/me", {"$select": "displayName,mail,userPrincipalName"})

    def events(self, days: int = DEFAULT_DAYS, since: datetime | None = None) -> list[dict]:
        """これからの予定（繰り返しも展開したもの）を、始まる順に。"""
        start = since or datetime.now()
        got = self._get("/me/calendarView", {
            "startDateTime": start.isoformat(timespec="seconds"),
            "endDateTime": (start + timedelta(days=max(days, 1))).isoformat(timespec="seconds"),
            "$select": "id,subject,start,end,location,organizer,isAllDay,isCancelled,webLink,showAs",
            "$orderby": "start/dateTime",
            "$top": MAX_EVENTS,
        })
        return [_event(e) for e in (got.get("value") or []) if not e.get("isCancelled")]


def _event(raw: dict) -> dict:
    """Graph の予定を、そのまま人に見せられる短い形にする。"""
    return {
        "id": raw.get("id", ""),
        "subject": raw.get("subject") or "（件名なし）",
        "start": (raw.get("start") or {}).get("dateTime", ""),
        "end": (raw.get("end") or {}).get("dateTime", ""),
        "all_day": bool(raw.get("isAllDay")),
        "location": ((raw.get("location") or {}).get("displayName") or "").strip(),
        "organizer": (((raw.get("organizer") or {}).get("emailAddress") or {}).get("name") or "").strip(),
        "free": raw.get("showAs") == "free",
        "url": raw.get("webLink", ""),
    }


# 最初の1回の許可（OAuth 2.0 + PKCE）


class _Callback(BaseHTTPRequestHandler):
    code: str = ""
    state: str = ""

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Callback.code = (query.get("code") or [""])[0]
        _Callback.state = (query.get("state") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = ("Kei Agent（仕事）が Outlook の予定を読めるようになりました。ターミナルに戻ってください。"
                   if _Callback.code else "許可が取れませんでした。ターミナルの表示を見てください。")
        self.wfile.write(f"<html><body style='font-family:sans-serif'><p>{message}</p></body></html>".encode())

    def log_message(self, *args) -> None:
        return


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def login(app: App | None = None, path: Path | None = None, open_browser: bool = True) -> Tokens:
    """ブラウザで1回だけ許可してもらい、リフレッシュトークンを保存する。"""
    app = app or App.from_env()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    url = f"{app.authorize_url}?" + urllib.parse.urlencode({
        "client_id": app.client_id, "response_type": "code", "redirect_uri": app.redirect_uri,
        "response_mode": "query", "scope": " ".join(SCOPES), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    parsed = urllib.parse.urlparse(app.redirect_uri)
    server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), _Callback)
    server.timeout = 300
    print(f"ブラウザで Microsoft の許可画面を開きます。会社のアカウントで入って「承諾」を押してください:\n{url}\n")
    if open_browser:
        os.system(f"open {url!r}")
    _Callback.code = _Callback.state = ""
    server.handle_request()
    server.server_close()
    if not _Callback.code:
        raise GraphError("許可が取れませんでした（画面で「承諾」を押してから、もう一度）")
    if _Callback.state != state:
        raise GraphError("返ってきた state が違います（別の許可画面の結果かもしれません）")
    got = _post(app.token_url, {
        "grant_type": "authorization_code", "code": _Callback.code, "client_id": app.client_id,
        "redirect_uri": app.redirect_uri, "code_verifier": verifier, "scope": " ".join(SCOPES)})
    tokens = Tokens(refresh_token=got.get("refresh_token", ""), access_token=got["access_token"],
                    expires_at=time.time() + float(got.get("expires_in") or 3600))
    if not tokens.refresh_token:
        raise GraphError("リフレッシュトークンが返りませんでした（scope に offline_access が要ります）")
    write_tokens(tokens, path)
    return tokens


def probe(graph: Graph) -> None:
    """読めるかどうかを確かめる。"""
    me = graph.me()
    print(f"入れたアカウント: {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>")
    events = graph.events(days=7)
    print(f"これから7日の予定: {len(events)} 件")
    for event in events[:10]:
        where = f"（{event['location']}）" if event["location"] else ""
        print(f"  {event['start'][:16].replace('T', ' ')} {event['subject']}{where}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-ms-login",
                                     description="Outlook の予定を読む許可を1回だけ取る（読み取り専用）")
    parser.add_argument("--probe", action="store_true", help="許可済みのトークンで、読めるかどうかを確かめる")
    parser.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.probe:
            probe(Graph.load())
            return
        login(open_browser=not args.no_open)
        print(f"許可を保存しました: {token_path()}")
        print("確かめるには: uv run --group work kei-agent-ms-login --probe")
    except GraphError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
