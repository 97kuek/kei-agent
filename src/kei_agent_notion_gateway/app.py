"""研究ホームだけを操作できる Notion の MCP サーバー（127.0.0.1:8791/mcp）。

研究 Claude には Notion のトークンを渡さない。代わりに、このプロセスだけが `NOTION_TOKEN` を持ち、
研究ホームの中かどうかを確かめてから Notion を呼ぶ。研究 Claude が持つのは、この入口の合言葉
（`KEI_AGENT_NOTION_GATEWAY_TOKEN`）だけで、それは Notion の API には使えない。

- 127.0.0.1 でしか待ち受けない
- `/health` 以外は Bearer 認証が要る
- 合言葉が空なら起動しない
- tool は下の11個だけ。任意の method と path を受け取る tool は置かない
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
from hmac import compare_digest

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError
from kei_agent_notion_gateway.config import GatewayConfig, load_gateway_config
from kei_agent_notion_gateway.scope import ResearchScope, ScopeError
from kei_agent_notion_gateway.service import ResearchNotion

log = logging.getLogger("kei-agent-notion-gateway")

HEALTH_PATH = "/health"
MCP_PATH = "/mcp"
# Notion の失敗を Claude へ返すときの長さの上限
ERROR_LIMIT = 300
INSTRUCTIONS = """研究ホーム配下の Notion を操作する。

対象はページ ID・ブロック ID・データソース ID で指定する。研究ホームの外を指すと、
どの操作も「研究ホーム外の対象です」で失敗する。別の経路を探さず、そのまま依頼者に伝えること。"""


def guarded(fn):
    """研究ホーム外と Notion の失敗を、Claude が読める形の失敗にする。

    そのまま外へ出すと「tool が壊れた」としか伝わらず、Claude が別の Notion 連携を探しはじめる。
    ここで理由を返し、代わりの経路がないことを分からせる。
    """
    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ScopeError as e:
            raise ToolError(f"{e}。研究ホームの外は操作できません（ほかの経路もありません）") from None
        except NotionError as e:
            raise ToolError(f"Notion を呼べませんでした: {str(e)[:ERROR_LIMIT]}") from None
    return run


def build_mcp(service: ResearchNotion) -> MCPServer:
    """研究ホーム用の tool だけを載せた MCP サーバー。"""
    mcp = MCPServer("research-notion", instructions=INSTRUCTIONS)

    @mcp.tool(description="研究ホーム配下のページ・ブロック・データベースを読む（直下の子ブロックも返す）")
    @guarded
    def read(target_id: str) -> dict:
        return service.read(target_id)

    @mcp.tool(description="研究ホーム配下を検索する（ホームの外は結果から外れる）")
    @guarded
    def search(query: str) -> list[dict]:
        return service.search(query)

    @mcp.tool(description="データソース（データベースの中身）を絞って読む")
    @guarded
    def query(data_source_id: str, filter: dict | None = None,  # Notion の body のキーに合わせる
              sorts: list[dict] | None = None) -> list[dict]:
        return service.query(data_source_id, filter, sorts)

    @mcp.tool(description="親ページの下にページを作る")
    @guarded
    def create_page(parent_id: str, properties: dict, children: list[dict] | None = None) -> dict:
        return service.create_page(parent_id, properties, children)

    @mcp.tool(description="ページのプロパティを直す")
    @guarded
    def update_page(target_id: str, properties: dict) -> dict:
        return service.update_page(target_id, properties)

    @mcp.tool(description="ページやブロックの下に本文のブロックを足す")
    @guarded
    def append_blocks(target_id: str, children: list[dict]) -> dict:
        return service.append_blocks(target_id, children)

    @mcp.tool(description="ページをアーカイブする（Notion のゴミ箱に入れる）")
    @guarded
    def archive(target_id: str) -> dict:
        return service.archive(target_id)

    @mcp.tool(description="ページを別の親ページの下へ移す")
    @guarded
    def move(source_id: str, destination_id: str) -> dict:
        return service.move(source_id, destination_id)

    @mcp.tool(description="ページを別の親ページの下へ複製する")
    @guarded
    def duplicate(source_id: str, destination_id: str) -> dict:
        return service.duplicate(source_id, destination_id)

    @mcp.tool(description="親ページの下にデータベースを作る")
    @guarded
    def create_database(parent_id: str, title: str, properties: dict) -> dict:
        return service.create_database(parent_id, title, properties)

    @mcp.tool(description="データベースの列（スキーマ）を足す・直す")
    @guarded
    def update_database(data_source_id: str, properties: dict) -> dict:
        return service.update_database(data_source_id, properties)

    return mcp


class BearerAuth:
    """`/health` 以外に Bearer 認証を求める。合言葉は定数時間で比べる。"""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        given = Headers(scope=scope).get("authorization", "")
        if not compare_digest(given, self.expected):
            await PlainTextResponse("合言葉が違います", status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(settings: GatewayConfig, service: ResearchNotion) -> Starlette:
    mcp = build_mcp(service)

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, json_response=True, host=settings.host)
    app.add_middleware(BearerAuth, token=settings.token)
    return app


def home_page_id(state_path) -> str:
    """研究ホームの root。kei-agent-notion-setup が notion.json に書いたもの。"""
    if not state_path.exists():
        raise RuntimeError(f"{state_path} がありません。kei-agent-notion-setup を先に実行してください")
    try:
        home = json.loads(state_path.read_text(encoding="utf-8")).get("home_page_id") or ""
    except ValueError as e:
        raise RuntimeError(f"{state_path} を読めません: {e}") from None
    if not home:
        raise RuntimeError(f"{state_path} に home_page_id がありません")
    return str(home)


def build_service(settings: GatewayConfig) -> ResearchNotion:
    notion = Notion(settings.notion_token)
    return ResearchNotion(notion, ResearchScope(notion, home_page_id(settings.state_path)), log)


def main() -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_gateway_config(load_config(), dict(os.environ))
        service = build_service(settings)
    except (RuntimeError, NotionError, ScopeError) as e:
        print(f"起動できません: {e}", file=sys.stderr)
        return 1
    log.info("Notion gateway を始めます: http://%s:%d%s", settings.host, settings.port, MCP_PATH)
    uvicorn.run(build_app(settings, service), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
