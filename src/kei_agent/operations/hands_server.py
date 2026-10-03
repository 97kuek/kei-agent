"""MCP サーバーを、本体のプロセスの中で開く。AI を動かす道具は conversation/hands.py、読む道具の材料は
scheduling/materials.py。

住所は config.toml の [hands] url（127.0.0.1 だけ）、合言葉は秘密情報の KEI_AGENT_HANDS_TOKEN。
合言葉が違う呼び出しは、道具に届く前に断る。どちらかが無ければ、サーバーは起動しない。
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
# OAuth の案内の場所。このサーバーは OAuth を使わないので、合言葉なしでも「無い」（404）と答える
# （OpenAI のトンネルの点検は、合言葉を付けずにここを見て、404 なら OAuth なしのサーバーと見なす）
WELL_KNOWN = "/.well-known/"
INSTRUCTIONS = (
    "Kei Agent の MCP サーバー。作業場（研究テーマ・プロジェクト・仕事）で Claude Code か Codex を動かす。"
    "まず workspaces で頼める作業場を見て、run で頼む。重さは light（抜き出し・要約）・normal（ふつうの作業）・"
    "deep（設計・計画・厳密な見直し）。比べる・絞るなど決まった仕事は use_case で名前で選べる。"
    "仕事（会社のデータ。仕事の担当と、そのプロジェクト work-*）は外へ出られないので、外の情報が要るなら依頼元の MCP クライアントで調べて request に入れる。"
    "続きを頼むときは、前の結果の conversation を渡す。"
    "run の依頼ごとに client_request_id を作り、応答喪失の再送では同じIDと同じ引数を使う。新しい依頼・続きには新しいIDを使う。"
    "長い会話を区切るときは handoff に workspace と conversation を渡し、返った新しい conversation で run する。"
    "Daily・締切・振り返りの材料は、読む道具（agenda・reading・recent・jobs）で読む。"
    "Kei Agent からの知らせを notices で読み、利用者が指定・許可した宛先へ配信する。配信成功した ID だけ notices の done に渡す。"
    "status が accepted なら、あとで status に ticket を渡して結果を見る。needs_input なら、本文の確認に答えて、"
    "同じ conversation で run する。担当・アカウント・届く範囲は作業場から決まり、変えられない"
)


def build_mcp(hands: Hands) -> MCPServer:
    """道具はどれも async にする（async でない道具は別のスレッドで動き、記録の SQLite が使えない）。
    戻り値は dict[str, Any] と書く（決まった項目のまま MCP クライアントに渡る。ただの dict だと、文字にした JSON だけになる）。"""
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
                          "client_request_id は任意の依頼ID。同じID・同じ引数は元ticketを返し、異なる引数は拒否。省略時は毎回新規実行。"
                          "返すのは status（done・needs_input・failed・accepted）・text（結果の説明）・conversation・"
                          "files（作業場の outputs にできたファイル）・ticket・phase・elapsed_seconds")
    async def run(workspace: str, request: str, weight: str = "normal", engine: str = "",
                  conversation: str = "", use_case: str = "", read_only: bool = False,
                  minutes: int = 0, client_request_id: str = "") -> dict[str, Any]:
        try:
            return await hands.run(workspace, request, weight, engine, conversation, use_case, read_only, minutes,
                                   client_request_id)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(description="既存の会話を読むだけでまとめ、新しい会話へ引き継ぐ。workspace と conversation は run の結果を使う。"
                          "返すのは status・text・memo（引き継ぎの要点）・conversation（新会話）・source_conversation（前の会話）・"
                          "ticket・phase・elapsed_seconds。accepted なら status に ticket を渡して待つ。"
                          "同じ前の会話を再指定しても、新会話は増えない。中断・失敗した場合は同じ引数で手動で頼み直す。"
                          "done の新しい conversation で run すると、最初の依頼へメモを渡す")
    async def handoff(workspace: str, conversation: str) -> dict[str, Any]:
        try:
            return await hands.handoff(workspace, conversation)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ_ONLY,
              description="受付番号（run・handoff が返した ticket）の作業の様子と結果。status が running なら未完了。phase は queued（待機中）・running（実行中）・done・needs_input・failed。elapsed_seconds は受付からの秒数")
    async def status(ticket: str) -> dict[str, Any]:
        try:
            return hands.status(ticket)
        except HandsError as e:
            raise ToolError(str(e)) from None


    @mcp.tool(description="研究テーマかプロジェクト（work-<名前> など）の作業場を作る。folder を渡すと既存のフォルダを使う。研究テーマは研究ホームにも登録する。"
                          "もうあれば作らない。返すのは name・kind・folder・created")
    async def create_workspace(name: str, folder: str = "") -> dict[str, Any]:
        try:
            return await hands.create_workspace(name, folder)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(description="時間を測る（Toggl）。action は start（domain は research・course・work、label はテーマや科目の名前。"
                          "動いている計測は止める）・stop・status・memo・resolve。memo は entry_id の記録にメモを追記する"
                          "（entry_id を省略すると現在の計測）。resolve は利用者が送信先を確認した停止済み記録の entry_id と"
                          "resolution を渡す。recorded は Toggl に既にある、missing は無いと確認した場合の再送。"
                          "未確認で再送しない。返すのは running・stopped・needs_review。記録は id・description・started・minutes・"
                          "memo・toggl_state・notion_state。止めた記録は Toggl と Notion の時間記録に送る")
    async def timer(action: str, domain: str = "", label: str = "", entry_id: str = "", memo: str = "",
                    resolution: str = "") -> dict[str, Any]:
        try:
            return await hands.timer(action, domain, label, entry_id, memo, resolution)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(description="Moodle の課題提出・小テスト受験終了を認証付き API で確認し、Notion の課題を提出済みにする。"
                          "AI は使わない。未提出・取得失敗で状態を戻さない。最大10件、残りは次回。"
                          "返すのは ok・text・enabled（API設定済みか）・completed・checked・skipped・errors・next_id")
    async def sync_submissions() -> dict[str, Any]:
        try:
            return await hands.sync_submissions()
        except HandsError as error:
            raise ToolError(str(error)) from None

    @mcp.tool(description="Mac が以前配信した読みものを URL で指定して Notion に保存する互換窓口。saved=false は保存を解除する。"
                          "URL は reading で取得したものを使い、題名で推測しない。クライアントが独自に見つけた記事は、そのクライアントの保存機能を使う。"
                          "返すのは url・saved・liked・errors。errors があれば保存成功として扱わない")
    async def save_reading(url: str, saved: bool = True) -> dict[str, Any]:
        try:
            return await hands.save_reading(url, saved)
        except HandsError as error:
            raise ToolError(str(error)) from None

    @mcp.tool(description="声のスイッチ。notify は出来事（作業の終わり・締切など）を声で知らせるか、listen はマイクで会話するか。"
                          "渡さなかったほうは変えない（何も渡さなければ今の様子）。返すのは notify・listen")
    async def voice(notify: bool | None = None, listen: bool | None = None) -> dict[str, Any]:
        try:
            return await hands.voice(notify, listen)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(description="作業場（研究テーマ・プロジェクト）の inputs/ に、文のファイルを置く（依頼に添付された文など）。"
                          "name はフォルダを含まない名前、content は中身。返す path を run の頼みごとに書いて伝える")
    async def put_file(workspace: str, name: str, content: str) -> dict[str, Any]:
        try:
            return hands.put_file(workspace, name, content)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ_ONLY,
              description="作業場の outputs/ にできたファイル（run の files に出たもの）の中身。文のファイルだけ。"
                          "path は run の files の path。返すのは path・text・truncated（長すぎて切ったか）")
    async def read_file(workspace: str, path: str) -> dict[str, Any]:
        try:
            return hands.read_file(workspace, path)
        except HandsError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(description="Kei Agent の通知（ジョブが終わった・困りごと・課題の新着など）のうち、"
                          "まだ出していないもの（古い順）。done=[] で呼んで読み、宛先に配信できたものの id を done に入れてもう一度呼ぶと、"
                          "それは次から返さない（done に入れなかったものは、次の回にもう一度返る）。読むだけでは確認済みにしない。"
                          "notices の1件は id・channel（出すつもりだった"
                          "チャンネル）・thread_ts・thread（スレッドの親の本文）・text・at。配信は呼び出し元の MCP クライアントが担当する")
    async def notices(done: list[str] | None = None) -> dict[str, Any]:
        return hands.notices(done)

    assistant = hands.assistant

    @mcp.tool(annotations=READ_ONLY,
              description="これから days 日（1〜14。1 なら今日だけ）の授業・会議・締切を時刻の早い順に。"
                          "items の kind は 授業・会議・締切、date・start・end・title・where（場所）・course・url・join_url（参加リンク）・passcode（会議のパスコード）・"
                          "agent（予定を読んだ担当）。unread は予定を読めなかった担当")
    async def agenda(days: int = 1) -> dict[str, Any]:
        return await materials.agenda(assistant, days)

    @mcp.tool(annotations=READ_ONLY,
              description="この days 日（1〜7）の Mac の旧配信分の読みもの（新しい順）。title・url・source・summary・"
                          "why（選んだ理由）・liked（依頼者が 👍 した）・saved（Notion の読みものに入れた）")
    async def reading(days: int = 1) -> dict[str, Any]:
        return await materials.reading(assistant, days)

    @mcp.tool(annotations=READ_ONLY,
              description="この hours 時間（1〜168）の動き。threads（やり取りのあった Slack のスレッド。研究テーマは"
                          "最初の頼みごとと最後の答えの抜き出しつき）・runs（担当ごとの実行と失敗の数）・"
                          "hands（MCP で受けた頼みごと）・jobs（終わったジョブ）")
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
    """本体のプロセスの中で、MCP を開く（止められるまで待つ）。開けなくても本体は止めない。"""
    if not token:
        await assistant.notify_trouble("MCP の合言葉（KEI_AGENT_HANDS_TOKEN）が無いので、サーバーを起動できませんでした")
        return
    await serve_loopback(assistant, url, "MCP", lambda host: build_app(Hands(assistant), token, host))
