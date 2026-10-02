"""手の口（MCP）を、本体のプロセスの中で開く。中身は hands.py。

住所は config.toml の [hands] url（127.0.0.1 だけ）、合言葉は秘密情報の KEI_AGENT_HANDS_TOKEN。
合言葉が違う呼び出しは、道具に届く前に断る。どちらかが無ければ、口は開かない。
"""

from __future__ import annotations

import hmac
import logging
import urllib.parse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from kei_agent.conversation.hands import Hands, HandsError

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
# 読むだけの道具の印（ChatGPT は、この印の無い道具を書き込みとして扱い、プランによっては使わせない）
READ_ONLY = ToolAnnotations(read_only_hint=True)
HEALTH_PATH = "/health"
INSTRUCTIONS = (
    "Kei Agent の手。作業場（研究テーマ・プロジェクト・大学・仕事・知識）で Claude Code か Codex を動かす。"
    "まず workspaces で頼める作業場を見て、run で頼む。重さは light（抜き出し・要約）・normal（ふつうの作業）・"
    "deep（設計・計画・厳密な見直し）。続きを頼むときは、前の結果の conversation を渡す。"
    "status が accepted なら、あとで status に ticket を渡して結果を見る。needs_input なら、本文の確認に答えて、"
    "同じ conversation で run する。担当・アカウント・届く範囲は作業場から決まり、変えられない"
)


def build_mcp(hands: Hands) -> MCPServer:
    """道具はどれも async にする（async でない道具は別のスレッドで動き、記録の SQLite が使えない）。"""
    mcp = MCPServer("kei-agent-hands", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY,
              description="頼める作業場の一覧。name（run に渡す）・kind（研究テーマ・プロジェクト・担当）・"
                          "agent（受け持つ担当）・engines（選べる AI）・weights（選べる重さ）")
    async def workspaces() -> dict:
        return {"workspaces": hands.workspaces()}

    @mcp.tool(description="作業場で AI を動かす。workspace は workspaces の name、request は頼みごと、"
                          "weight は light・normal・deep、engine は claude・codex（空なら作業場の既定）、"
                          "conversation は続きを頼むときの番号（空なら新しい会話）。"
                          "返すのは status（done・needs_input・failed・accepted）・text・conversation・"
                          "files（作業場の outputs にできたファイル）・ticket")
    async def run(workspace: str, request: str, weight: str = "normal", engine: str = "",
                  conversation: str = "") -> dict:
        try:
            return await hands.run(workspace, request, weight, engine, conversation)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ_ONLY,
              description="受付番号（run が返した ticket）の作業の様子と結果。status が running なら、まだ動いている")
    async def status(ticket: str) -> dict:
        try:
            return hands.status(ticket)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return mcp


class TokenAuth:
    """Authorization: Bearer <合言葉> を確かめる。合わなければ 401（道具には届かない）。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        given = Headers(scope=scope).get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, self.token):
            await JSONResponse({"error": "Kei Agent: 合言葉が違います"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(hands: Hands, token: str, host: str):
    app = build_mcp(hands).streamable_http_app(streamable_http_path=MCP_PATH, json_response=True, host=host)
    app.add_middleware(TokenAuth, token=token)
    return app


async def serve(assistant, url: str, token: str) -> None:
    """本体のプロセスの中で、手の口を開く（止められるまで待つ）。開けなくても本体は止めない。"""
    import uvicorn

    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        await assistant.notify_trouble(f"手の口は 127.0.0.1 のポートで指定してください: {url}")
        return
    if not token:
        await assistant.notify_trouble("手の口の合言葉（KEI_AGENT_HANDS_TOKEN）が無いので、口を開けませんでした")
        return
    app = build_app(Hands(assistant), token, parsed.hostname)
    server = uvicorn.Server(uvicorn.Config(app, host=parsed.hostname, port=parsed.port, log_level="warning"))
    log.info("手の口を開きます（%s%s）", url.rstrip("/"), MCP_PATH)
    try:
        await server.serve()
    except SystemExit:
        log.error("手の口を開けませんでした（%s）", url)
        await assistant.notify_trouble(f"手の口（{url}）を開けませんでした")
