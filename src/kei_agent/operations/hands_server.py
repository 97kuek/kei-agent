"""手の口（MCP）を、本体のプロセスの中で開く。AI を動かす道具は conversation/hands.py、読む道具の材料は
scheduling/materials.py。

住所は config.toml の [hands] url（127.0.0.1 だけ）、合言葉は秘密情報の KEI_AGENT_HANDS_TOKEN。
合言葉が違う呼び出しは、道具に届く前に断る。どちらかが無ければ、口は開かない。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kei_agent.conversation.hands import Hands, HandsError
from kei_agent.conversation.loopback import serve_loopback
from kei_agent.scheduling import materials

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
# 読むだけの道具の印（ChatGPT は、この印の無い道具を書き込みとして扱い、プランによっては使わせない）
READ_ONLY = ToolAnnotations(read_only_hint=True)
HEALTH_PATH = "/health"
# OAuth の案内の場所。この口は OAuth を使わないので、合言葉なしでも「無い」（404）と答える
# （OpenAI のトンネルの点検は、合言葉を付けずにここを見て、404 なら OAuth なしの口と見なす）
WELL_KNOWN = "/.well-known/"
INSTRUCTIONS = (
    "Kei Agent の手。作業場（研究テーマ・プロジェクト・大学・仕事・知識）で Claude Code か Codex を動かす。"
    "まず workspaces で頼める作業場を見て、run で頼む。重さは light（抜き出し・要約）・normal（ふつうの作業）・"
    "deep（設計・計画・厳密な見直し）。比べる・絞るなど決まった仕事は use_case で名前で選べる。"
    "仕事・仕事の開発（会社のデータ）は外へ出られないので、外の情報が要るなら頭が調べて request に入れる。"
    "続きを頼むときは、前の結果の conversation を渡す。"
    "Daily・締切・振り返りの材料は、読む道具（agenda・reading・recent・jobs）で読む。"
    "status が accepted なら、あとで status に ticket を渡して結果を見る。needs_input なら、本文の確認に答えて、"
    "同じ conversation で run する。担当・アカウント・届く範囲は作業場から決まり、変えられない"
)


def build_mcp(hands: Hands) -> MCPServer:
    """道具はどれも async にする（async でない道具は別のスレッドで動き、記録の SQLite が使えない）。
    戻り値は dict[str, Any] と書く（決まった項目のまま頭に渡る。ただの dict だと、文字にした JSON だけになる）。"""
    mcp = MCPServer("kei-agent-hands", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY,
              description="頼める作業場の一覧。name（run に渡す）・kind（研究テーマ・プロジェクト・担当）・"
                          "agent（受け持つ担当）・engines（選べる AI）・weights（選べる重さ）・"
                          "use_cases（名前で選べる用途。manual はいちばん強いモデルなど）・max_minutes（上限時間の最大）")
    async def workspaces() -> dict[str, Any]:
        return {"workspaces": hands.workspaces()}

    @mcp.tool(description="作業場で AI を動かす。workspace は workspaces の name、request は頼みごと、"
                          "weight は light・normal・deep、engine は claude・codex（空なら作業場の既定）、"
                          "conversation は続きを頼むときの番号（空なら新しい会話）。"
                          "use_case は重さの代わりに用途を名前で選ぶとき（workspaces の use_cases）、"
                          "read_only は読むだけで書かない・動かさないとき、minutes は上限時間（workspaces の max_minutes まで）。"
                          "返すのは status（done・needs_input・failed・accepted）・text（頭への報告）・conversation・"
                          "files（作業場の outputs にできたファイル）・ticket")
    async def run(workspace: str, request: str, weight: str = "normal", engine: str = "",
                  conversation: str = "", use_case: str = "", read_only: bool = False,
                  minutes: int = 0) -> dict[str, Any]:
        try:
            return await hands.run(workspace, request, weight, engine, conversation, use_case, read_only, minutes)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ_ONLY,
              description="受付番号（run が返した ticket）の作業の様子と結果。status が running なら、まだ動いている")
    async def status(ticket: str) -> dict[str, Any]:
        try:
            return hands.status(ticket)
        except HandsError as e:
            raise ToolError(str(e)) from None

    assistant = hands.assistant

    @mcp.tool(annotations=READ_ONLY,
              description="これから days 日（1〜14。1 なら今日だけ）の授業・会議・締切を時刻の早い順に。"
                          "items の kind は 授業・会議・締切、date・start・end・title・where（場所）・course・url・"
                          "agent（予定を読んだ担当）。unread は予定を読めなかった担当")
    async def agenda(days: int = 1) -> dict[str, Any]:
        return await materials.agenda(assistant, days)

    @mcp.tool(annotations=READ_ONLY,
              description="この days 日（1〜7）に知識の担当が出した読みもの（新しい順）。title・url・source・summary・"
                          "why（選んだ理由）・liked（依頼者が 👍 した）・saved（Notion の読みものに入れた）")
    async def reading(days: int = 1) -> dict[str, Any]:
        return await materials.reading(assistant, days)

    @mcp.tool(annotations=READ_ONLY,
              description="この hours 時間（1〜168）の動き。threads（やり取りのあった Slack のスレッド。研究テーマは"
                          "最初の頼みごとと最後の答えの抜き出しつき）・runs（担当ごとの実行と失敗の数）・"
                          "hands（手の口で受けた頼みごと）・jobs（終わったジョブ）")
    async def recent(hours: int = 24) -> dict[str, Any]:
        return materials.recent(assistant, hours)

    @mcp.tool(annotations=READ_ONLY,
              description="研究のジョブ。active（待っている・動いている）と finished（2日のうちに終わった）。"
                          "id・name・workspace・status・detail・submitted・started・finished")
    async def jobs() -> dict[str, Any]:
        return materials.jobs(assistant)

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return mcp


class TokenAuth:
    """Authorization: Bearer <合言葉> を確かめる。合わなければ 401（道具には届かない）。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.strip()

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path.startswith(WELL_KNOWN):
            # 合言葉があっても無くても、本文の無い 404（本文があると、トンネルが OAuth の案内として読もうとして警告を出す）
            await Response(status_code=404)(scope, receive, send)
            return
        if scope["type"] == "lifespan" or (scope["type"] == "http" and path.rstrip("/") == HEALTH_PATH):
            await self.app(scope, receive, send)
            return
        scheme, _, given = Headers(scope=scope).get("authorization", "").strip().partition(" ")
        if scheme.lower() != "bearer" or not given.strip() or not hmac.compare_digest(given.strip(), self.token):
            await JSONResponse({"error": "Kei Agent: 合言葉が違います"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(hands: Hands, token: str, host: str):
    app = build_mcp(hands).streamable_http_app(streamable_http_path=MCP_PATH, json_response=True, host=host)
    app.add_middleware(TokenAuth, token=token)
    return app


async def serve(assistant, url: str, token: str) -> None:
    """本体のプロセスの中で、手の口を開く（止められるまで待つ）。開けなくても本体は止めない。"""
    if not token:
        await assistant.notify_trouble("手の口の合言葉（KEI_AGENT_HANDS_TOKEN）が無いので、口を開けませんでした")
        return
    await serve_loopback(assistant, url, "手の口", lambda host: build_app(Hands(assistant), token, host))
