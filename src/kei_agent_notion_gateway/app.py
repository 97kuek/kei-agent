"""Kei Agent の Notion ゲートウェイ（127.0.0.1:8791）。Notion に届くのはこのプロセスだけ。

`NOTION_TOKEN` を持つのはこのプロセスだけで、ほかのプロセスと LLM は client ごとの合言葉で
ここを呼ぶ。どのホームに届くかは合言葉（client）で決まり、要求ごとにここで確かめる。

- 127.0.0.1 でしか待ち受けない。`/health` 以外は client の合言葉が要る（親の合言葉そのものは通さない）
- `/mcp` … LLM 向けの道具。名前と形はどの client でも同じ
- `/notion/v1/…` … 決まった処理（Python）向けに Notion の API をそのまま中継する。研究（research）は使えない
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import sys
import urllib.parse

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kei_agent import version
from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError
from kei_agent_notion_gateway.clients import PROXY_CLIENTS, Tokens
from kei_agent_notion_gateway.config import GatewayConfig, load_gateway_config
from kei_agent_notion_gateway.gateway import Gateway, error_body
from kei_agent_notion_gateway.rules import Refused
from kei_agent_notion_gateway.scope import ScopeError
from kei_agent_notion_gateway.service import NotionTools, bounded

log = logging.getLogger("kei-agent-notion-gateway")

HEALTH_PATH = "/health"
MCP_PATH = "/mcp"
PROXY_PATH = "/notion/v1"
# 認証した client の名前を、要求の scope に置くキー
CLIENT_KEY = "kei_agent_notion_client"
# Notion の失敗を LLM へ返すときの長さの上限
ERROR_LIMIT = 300
INSTRUCTIONS = """Kei Agent の Notion。呼び出し元に許されたホーム（研究は研究ホーム、大学は授業ホーム）の中だけを操作できる。

対象はページ・データベース・データソース・ブロックの ID か Notion の URL で指定する。ホームの外を指すと、
どの操作も「届きません」で失敗する。別の経路を探さず、そのまま依頼者に伝えること。"""


def _json_response(status: int, code: str, message: str) -> Response:
    return Response(error_body(status, code, message), status_code=status, media_type="application/json")


def build_mcp(tools: NotionTools, default_client: str = "") -> MCPServer:
    """道具を載せた MCP サーバー。client は要求の合言葉で決まる（HTTP を通さないテストだけ default_client）。"""
    mcp = MCPServer("kei-agent-notion", instructions=INSTRUCTIONS)

    def run(ctx: Context, operation: str, *args) -> dict:
        request = ctx.request_context.request
        client = request.scope.get(CLIENT_KEY, "") if request is not None else default_client
        if not client:
            raise ToolError("ゲートウェイの合言葉を確かめられませんでした")
        try:
            return bounded(getattr(tools, operation)(client, *args))
        except ScopeError as e:
            raise ToolError(f"{e.item_id} には届きません（{e}）。このホームの外は操作できません"
                            "（ほかの経路もありません）") from None
        except NotionError as e:
            raise ToolError(f"Notion を呼べませんでした: {str(e)[:ERROR_LIMIT]}") from None
        except (OSError, http.client.HTTPException):
            raise ToolError("Notion につながりませんでした") from None
        except (Refused, ValueError) as e:
            raise ToolError(str(e)[:ERROR_LIMIT]) from None

    @mcp.tool(description="ページ・データベース・データソース・ブロックを読む。ページはプロパティと本文（Markdown）。"
                          "本文が長いときは next_cursor を cursor に渡して続きを読む。"
                          "blocks=true で本文をブロック ID つきの一覧にする（update_block などに使う）")
    def read(ctx: Context, target_id: str, cursor: str = "", blocks: bool = False) -> dict:
        return run(ctx, "read", target_id, cursor, blocks)

    @mcp.tool(description="タイトルで検索する（届くホームの外は結果に出ない）。filter は page か data_source")
    def search(ctx: Context, query: str, cursor: str = "", filter: str = "") -> dict:
        return run(ctx, "search", query, cursor, filter)

    @mcp.tool(description="データソース（データベースの中身）を絞り込んで読む。filter と sorts は Notion API の形。"
                          "データベースの ID でもよい")
    def query(ctx: Context, data_source_id: str, filter: dict | None = None,
              sorts: list[dict] | None = None, cursor: str = "") -> dict:
        return run(ctx, "query", data_source_id, filter, sorts, cursor)

    @mcp.tool(description="ページを作る。parent_id はページかデータソース（データベースの ID でもよい）。"
                          "properties は {列名: 値}（文字列・数値・一覧、または Notion の形）。content は Markdown の本文")
    def create_page(ctx: Context, parent_id: str, title: str = "", properties: dict | None = None,
                    content: str = "", icon: str = "") -> dict:
        return run(ctx, "create_page", parent_id, title, properties, content, icon)

    @mcp.tool(description="ページのプロパティ・アイコンを直す。in_trash=true でゴミ箱へ入れる（false で戻す）")
    def update_page(ctx: Context, page_id: str, properties: dict | None = None, icon: str | None = None,
                    in_trash: bool | None = None) -> dict:
        return run(ctx, "update_page", page_id, properties, icon, in_trash)

    @mcp.tool(description="ページやブロックの下に本文を足す。content は Markdown、blocks は Notion のブロックの形。"
                          "after にブロック ID を渡すと、その直後に入れる")
    def append_blocks(ctx: Context, target_id: str, content: str = "", blocks: list[dict] | None = None,
                      after: str = "") -> dict:
        return run(ctx, "append_blocks", target_id, content, blocks, after)

    @mcp.tool(description="ページの本文を Markdown で丸ごと置き換える（子ページとデータベースは消さない）")
    def replace_content(ctx: Context, page_id: str, content: str) -> dict:
        return run(ctx, "replace_content", page_id, content)

    @mcp.tool(description="ブロックを1つ直す。text で文字だけを替えるか、block に Notion の形"
                          "（例 {\"to_do\": {\"checked\": true}}）を渡す")
    def update_block(ctx: Context, block_id: str, text: str | None = None, block: dict | None = None) -> dict:
        return run(ctx, "update_block", block_id, text, block)

    @mcp.tool(description="ブロックを消す（ゴミ箱に入る）")
    def delete_block(ctx: Context, block_id: str) -> dict:
        return run(ctx, "delete_block", block_id)

    @mcp.tool(description="ページの下にデータベースを作る。properties は {列名: 型}（例 {\"名前\": \"title\", "
                          "\"期日\": \"date\"}）、選択肢の一覧（select になる）、または Notion の形")
    def create_database(ctx: Context, parent_id: str, title: str, properties: dict, description: str = "") -> dict:
        return run(ctx, "create_database", parent_id, title, properties, description)

    @mcp.tool(description="データソースの列（スキーマ）や名前を直す。列の書き方は create_database と同じ。"
                          "列を消すときは値を null にする")
    def update_data_source(ctx: Context, data_source_id: str, properties: dict | None = None,
                           title: str | None = None) -> dict:
        return run(ctx, "update_data_source", data_source_id, properties, title)

    @mcp.tool(description="ページを別の親（ページかデータソース）の下へ移す")
    def move(ctx: Context, page_id: str, new_parent_id: str) -> dict:
        return run(ctx, "move", page_id, new_parent_id)

    return mcp


class ClientAuth:
    """`/health` 以外は client の合言葉を求め、どの client かを要求の scope に置く。"""

    def __init__(self, app, tokens: Tokens):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        client = self.tokens.client(Headers(scope=scope).get("authorization", ""))
        if client is None:
            response = _json_response(401, "unauthorized", "Kei Agent gateway: 合言葉が違います")
            await response(scope, receive, send)
            return
        scope[CLIENT_KEY] = client
        await self.app(scope, receive, send)


def proxy_endpoint(gateway: Gateway):
    async def proxy(request: Request) -> Response:
        client = request.scope.get(CLIENT_KEY, "")
        if client not in PROXY_CLIENTS:
            return _json_response(403, "restricted_resource",
                                  f"Kei Agent gateway: {client} can't use the Notion API proxy")
        raw = await request.body()
        try:
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            return _json_response(400, "invalid_json", "Kei Agent gateway: 本文を JSON として読めません")
        query = urllib.parse.parse_qsl(request.url.query, keep_blank_values=True)
        status, data, headers = await asyncio.to_thread(
            gateway.call, client, request.method, "/" + request.path_params["path"], query, body)
        return Response(data, status_code=status, headers=headers, media_type="application/json")
    return proxy


def build_app(settings: GatewayConfig, gateway: Gateway) -> Starlette:
    mcp = build_mcp(NotionTools(gateway))

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        # version は起動したときの commit（deploy/update.sh が、新しい版で動いているかを見る）
        return JSONResponse({"ok": True, "version": version.RUNNING})

    app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, json_response=True, host=settings.host)
    app.add_route(PROXY_PATH + "/{path:path}", proxy_endpoint(gateway), methods=["GET", "POST", "PATCH", "DELETE"])
    app.add_middleware(ClientAuth, tokens=Tokens(settings.master))
    return app


def main() -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_gateway_config(load_config(), dict(os.environ))
    except RuntimeError as e:
        print(f"起動できません: {e}", file=sys.stderr)
        return 1
    # Notion の本物の鍵で Notion を呼ぶのは、ここだけ
    gateway = Gateway(Notion(settings.notion_token), settings.roots, log)
    homes = ", ".join(f"{client}={len(roots)}" for client, roots in settings.roots.items())
    log.info("Notion gateway を始めます: http://%s:%d（届くホームの数: %s）", settings.host, settings.port, homes)
    uvicorn.run(build_app(settings, gateway), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
