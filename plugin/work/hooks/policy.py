#!/usr/bin/env python3
"""仕事エージェントの二の柵（PreToolUse）。

会社の Microsoft 365 は読むだけ。メール送信、Teams 投稿、予定やファイルの作成・更新・削除を断る。
検索・取得・一覧・本文の閲覧は通す。
Microsoft 365 のほかのアカウントの連携（claude.ai のコネクタ、Codex の App）は使わない。
Notion は使わない（仕事には Notion を渡していない）。

Claude と Codex で道具の名前の書き方が違うので、両方を見る
（Codex は App の道具を `mcp__codex_apps__<App>__<道具>` と呼ぶ。Outlook はメールと予定で別の App）。
断る理由だけを stderr に出し、tool の中身は出さない。
"""

from __future__ import annotations

import json
import sys

ALLOW, DENY = 0, 2

# Microsoft 365 の道具（Claude は claude.ai の連携）と、Codex の App の道具（Microsoft の App は microsoft_ で始まる）
M365 = "mcp__claude_ai_Microsoft_365__"
CODEX_APPS = "mcp__codex_apps__"
# Notion はゲートウェイのほかは使わない（仕事には、そもそも Notion を渡していない）
GATEWAY_SERVERS = frozenset({"kei-notion", "kei_notion"})
# アカウントの連携（claude.ai のコネクタと Codex の App）。Microsoft 365 のほかは使わない
ACCOUNT_CONNECTORS = ("mcp__claude_ai_", CODEX_APPS)
# 名前にこれが入っていたら、外へ出す操作とみなす（Claude と Codex の、両方の道具の名前から決めた）
WRITE_WORDS = ("send", "create", "update", "delete", "modify", "move", "upload", "post",
               "reply", "forward", "respond", "trash", "untrash", "rename", "copy", "set_",
               "draft", "add_", "mark_", "schedule_", "unsubscribe", "cancel")


def mcp_server(tool: str) -> str:
    """`mcp__<server>__<tool>` の server。MCP の道具でなければ空文字。"""
    if not tool.startswith("mcp__"):
        return ""
    server, _, _name = tool[len("mcp__"):].partition("__")
    return server


def codex_app_tool(tool: str) -> str | None:
    """Codex の App の道具なら `<App>_<道具>`。フックには `mcp__codex_apps__<App>__<道具>` で、
    モデルには `mcp__codex_apps__<App>_<道具>` で見えるので、どちらも同じ形にそろえる。"""
    if not tool.startswith(CODEX_APPS):
        return None
    return tool[len(CODEX_APPS):].replace("__", "_")


def m365_tool(tool: str) -> str | None:
    """Microsoft 365 の道具なら、その名前（outlook_email_search など）。"""
    if tool.startswith(M365):
        return tool[len(M365):]
    app_tool = codex_app_tool(tool)
    if app_tool is not None and app_tool.startswith("microsoft_"):
        return app_tool[len("microsoft_"):]
    return None


def other_notion(tool: str) -> bool:
    """ゲートウェイではない Notion か（アカウントの Notion 連携や、名前に notion を含む別の MCP）。"""
    server = mcp_server(tool)
    if server == "codex_apps":
        return "notion" in tool.lower()
    return "notion" in server.lower() and server not in GATEWAY_SERVERS


def decide(event: dict) -> tuple[int, str]:
    tool = str(event.get("tool_name") or "")
    if not tool:
        return DENY, "どの道具を使うのか読み取れませんでした"
    if other_notion(tool):
        return DENY, "仕事では Notion を使いません"
    m365 = m365_tool(tool)
    if m365 is not None:
        if any(word in m365.lower() for word in WRITE_WORDS):
            return DENY, ("仕事のツールは読み取り専用です（送信・投稿・予定やファイルの変更はできません）。"
                          "文章案を返すところまでにして、送るのは依頼者に任せてください")
        return ALLOW, ""
    if tool.startswith(ACCOUNT_CONNECTORS):
        return DENY, "仕事エージェントは Microsoft 365 のほかの連携を使いません"
    return ALLOW, ""


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
    except ValueError:
        print("hook に渡された内容を読み取れませんでした", file=sys.stderr)
        return DENY
    if not isinstance(event, dict):
        print("hook に渡された内容を読み取れませんでした", file=sys.stderr)
        return DENY
    code, reason = decide(event)
    if reason:
        print(reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
