"""確かめてから Notion に送る、ゲートウェイの芯。中継の口（`/notion/v1`）も MCP の道具もここを通る。

要求1つごとに、触れる ID をすべて client のホームの中か確かめてから、1回だけ Notion に送る。
検索は送ったあとで、ホームの外の結果を落とす。

記録に残すのは、時刻・client・操作の名前・対象の ID・成否・失敗の種類だけ。本文、プロパティの値、
検索の結果、Notion の失敗の文、Authorization は残さない。
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.parse
from collections.abc import Sequence

from kei_agent.notion import Notion, NotionError, safe_to_resend
from kei_agent_notion_gateway.rules import Refused, plan
from kei_agent_notion_gateway.scope import Scope, ScopeError, Tree, parse_id

log = logging.getLogger("kei-agent-notion-gateway")
_CONNECTION_ERRORS = (OSError, http.client.HTTPException)


def error_body(status: int, code: str, message: str) -> bytes:
    """Notion と同じ形の失敗。呼んだ側の `NotionError` にそのまま載る。"""
    return json.dumps({"object": "error", "status": status, "code": code, "message": message},
                      ensure_ascii=False).encode()


def _error_kind(error: BaseException) -> str:
    if isinstance(error, Refused):
        return "refused"
    if isinstance(error, ScopeError):
        return "scope"
    if isinstance(error, NotionError):
        return f"notion:{error.status}" if error.status else "notion"
    return type(error).__name__


class Gateway:
    def __init__(self, notion: Notion, roots: dict[str, frozenset[str]], audit: logging.Logger = log,
                 tree: Tree | None = None):
        self.notion = notion
        self.roots = roots
        self.audit = audit
        self.tree = tree or Tree(notion)

    def scope(self, client: str) -> Scope:
        return Scope(client, self.roots.get(client, frozenset()), self.tree)

    def call(self, client: str, method: str, path: str, query: Sequence[tuple[str, str]] = (),
             body: object = None) -> tuple[int, bytes, dict[str, str]]:
        """中継の口から1回。断ったときも Notion と同じ形の返事にする。"""
        try:
            return self.send(client, method, path, query, body)
        except Refused as e:
            return 403, error_body(403, "restricted_resource", f"Kei Agent gateway: {e}"), {}
        except ScopeError as e:
            return 403, error_body(403, "restricted_resource",
                                   f"Kei Agent gateway: {client} can't reach {e.item_id}"), {}
        except NotionError:
            # 届く範囲を確かめる問い合わせが落ちた。何も書いていないので、送り直してよい
            return 503, error_body(503, "service_unavailable",
                                   "Kei Agent gateway: 届く範囲を Notion で確かめられませんでした"), {}
        except _CONNECTION_ERRORS:
            if safe_to_resend(method, path):
                return 502, error_body(502, "service_unavailable", "Kei Agent gateway: Notion に届きませんでした"), {}
            # 書き込みが済んだか分からないときは 5xx にしない（呼んだ側が送り直して二重に作るため）
            return 409, error_body(409, "conflict_error", "Kei Agent gateway: Notion との接続が切れました"
                                                          "（書き込みが済んだか分かりません）"), {}

    def request(self, client: str, method: str, path: str, body: object = None,
                query: Sequence[tuple[str, str]] = ()) -> dict:
        """MCP の道具から1回。Notion の失敗は状態つきの NotionError、ホームの外は ScopeError。"""
        status, data, _headers = self.send(client, method, path, query, body)
        try:
            payload = json.loads(data or b"{}")
        except ValueError:
            raise NotionError(f"{status} Notion の返事を JSON として読めません", status) from None
        if not 200 <= status < 300:
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            raise NotionError(f"{status} {message}", status)
        return payload

    def send(self, client: str, method: str, path: str, query: Sequence[tuple[str, str]] = (),
             body: object = None) -> tuple[int, bytes, dict[str, str]]:
        """触れる ID を確かめてから1回だけ送る。外なら ScopeError、分からない要求なら Refused。"""
        method = method.upper()
        # 断る前でも、何を頼まれたかは残す（パスの1段目が英小文字のときだけ。ID や中身は残さない）
        head = path.strip("/").split("/", 1)[0]
        operation, target = f"{method} /{head if re.fullmatch(r'[a-z_]{1,32}', head) else '?'}", ""
        try:
            checked = plan(method, path, query, body)
            operation = checked.operation
            target = parse_id(checked.targets[0][0]) if checked.targets else ""
            scope = self.scope(client)
            for item_id, kind in checked.targets:
                scope.require(item_id, kind)
            parts = [part for part in path.strip("/").split("/") if part]
            url = "/" + "/".join(parts) + (f"?{urllib.parse.urlencode(query)}" if query else "")
            status, data, headers = self.notion.forward(
                method, url, json.dumps(body).encode() if body is not None else None)
            ok = 200 <= status < 300
            if ok:
                for moved in checked.moved:
                    self.tree.forget(moved)
                if checked.search:
                    data = self._only_inside(scope, data)
        except (Refused, ScopeError, NotionError, *_CONNECTION_ERRORS) as e:
            self._record(client, operation, target, "", e)
            raise
        self._record(client, operation, target, "" if ok else str(status))
        return status, data, headers

    @staticmethod
    def _only_inside(scope: Scope, data: bytes) -> bytes:
        """検索の結果から、ホームの外のものを落とす。落とした数も返さない（何があるかが分かってしまう）。"""
        try:
            found = json.loads(data)
        except ValueError:
            raise NotionError("検索の返事を JSON として読めません") from None
        kinds = {"page": "page", "data_source": "data_source", "database": "database"}
        found["results"] = [item for item in found.get("results") or []
                            if isinstance(item, dict) and scope.allows(item, kinds.get(item.get("object"), "page"))]
        return json.dumps(found).encode()

    def _record(self, client: str, operation: str, target: str, status: str = "",
                error: BaseException | None = None) -> None:
        kind = _error_kind(error) if error is not None else status
        extra = {"client": client, "operation": operation, "target_id": target, "ok": not kind, "error_type": kind}
        if kind:
            self.audit.warning("%s %s %s 失敗（%s）", client, operation, target or "-", kind, extra=extra)
        else:
            self.audit.info("%s %s %s", client, operation, target or "-", extra=extra)


class ClientNotion:
    """1つの client として Gateway を呼ぶ窓口。`kei_agent.notion.Notion` と同じ呼び方にそろえる。"""

    def __init__(self, gateway: Gateway, client: str):
        self.gateway = gateway
        self.client = client

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        path, _, query = path.partition("?")
        return self.gateway.request(self.client, method, path, body, urllib.parse.parse_qsl(query))
