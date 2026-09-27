#!/usr/bin/env python3
"""大学エージェントの二の柵（PreToolUse）。

Box は読むだけ。アップロード・作成・移動・複製・更新・コメントを断る。
Notion はゲートウェイ（kei-notion）だけ。授業ホームの外へは、ゲートウェイが通さない。
アカウントに付いた Notion 連携など、ほかの Notion は断る（授業ホームの外まで届くため）。
Box のほかのアカウントの連携（claude.ai のコネクタ、Codex の App）は使わない。

Claude と Codex で道具の名前の書き方が違うので、両方を見る
（Codex は MCP の server の名前の `-` を `_` にし、App の道具を `mcp__codex_apps__<App>__<道具>` と呼ぶ）。
断る理由だけを stderr に出し、tool の中身は出さない。
"""

from __future__ import annotations

import json
import sys

ALLOW, DENY = 0, 2

# Box の道具（Claude は claude.ai の連携）と、Codex の App の道具
BOX = "mcp__claude_ai_Box__"
CODEX_APPS = "mcp__codex_apps__"
# 授業ホームの中だけに届く Notion（ゲートウェイ）。ほかの Notion は授業ホームの外まで届く
GATEWAY_SERVERS = frozenset({"kei-notion", "kei_notion"})
# アカウントの連携（claude.ai のコネクタと Codex の App）。Box のほかは使わない
ACCOUNT_CONNECTORS = ("mcp__claude_ai_", CODEX_APPS)
# Box で通す道具（読むだけ）。ここに無い Box の道具は、名前を問わず断る
BOX_READS = frozenset({
    "search_files_keyword", "search_folders_by_name", "search_files_metadata",
    "list_folder_content_by_folder_id", "list_file_comments", "list_item_collaborations",
    "list_metadata_templates", "list_tasks", "list_hubs", "get_hub_details", "get_hub_items",
    "get_file_details", "get_file_content", "get_file_preview", "get_preview_page",
    "get_folder_details", "get_metadata_template_schema", "who_am_i",
})


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


def box_tool(tool: str) -> str | None:
    """Box の道具なら、その名前（search_files_keyword など）。"""
    if tool.startswith(BOX):
        return tool[len(BOX):]
    app_tool = codex_app_tool(tool)
    if app_tool is not None and app_tool.startswith("box_"):
        return app_tool[len("box_"):]
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
        return DENY, ("大学の Notion は kei-notion のゲートウェイだけを使います"
                      "（ほかの Notion 連携は授業ホームの外まで届きます）")
    box = box_tool(tool)
    if box is not None:
        if box in BOX_READS:
            return ALLOW, ""
        return DENY, ("Box は読み取り専用です（アップロード・作成・移動・複製・更新はできません）。"
                      "残すものは Notion の授業ホームに置いてください")
    if tool.startswith(ACCOUNT_CONNECTORS):
        return DENY, "大学エージェントは Box のほかの連携を使いません（Notion はゲートウェイから）"
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
