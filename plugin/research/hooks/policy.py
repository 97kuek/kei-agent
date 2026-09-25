#!/usr/bin/env python3
"""研究エージェントの二の柵（PreToolUse）。

一の柵は `--allowedTools` と sandbox。ここで見るのは、そこをすり抜けうる2つだけ。

- 生の Notion トークン（`NOTION_TOKEN` など）に手を伸ばす Bash（研究 claude には渡していないが、置き場所を探されると困る）
- `research-notion` 以外の Notion（研究ホームの外まで届いてしまう）

ファイルと Bash の範囲は sandbox が見る。同じ判定をここに書き足さない。
断る理由だけを stderr に出し、tool の中身は出さない。
"""

from __future__ import annotations

import json
import re
import sys

ALLOW, DENY = 0, 2

# 研究ホームの外まで届く Notion。使ってよいのはゲートウェイ（mcp__research-notion__*）だけ
GATEWAY_SERVER = "research-notion"
# 生の Notion トークン（NOTION_TOKEN、NOTION_COURSE_TOKEN など）を読む・渡すコマンド。
# ゲートウェイの合言葉（KEI_AGENT_NOTION_GATEWAY_TOKEN）だけは研究 claude が持ってよい
RAW_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Z0-9_]*_)?NOTION_[A-Z0-9_]*TOKEN(?![A-Za-z0-9_])")
GATEWAY_TOKEN = "KEI_AGENT_NOTION_GATEWAY_TOKEN"
# コマンドが入りうるところ（Bash と、その仲間の tool）
COMMAND_KEYS = ("command", "cmd", "script")


def mcp_server(tool: str) -> str:
    """`mcp__<server>__<tool>` の server。MCP の道具でなければ空文字。"""
    if not tool.startswith("mcp__"):
        return ""
    server, _, _name = tool[len("mcp__"):].partition("__")
    return server


def raw_token(command: str) -> bool:
    return any(match.group(0) != GATEWAY_TOKEN for match in RAW_TOKEN.finditer(command))


def decide(event: dict) -> tuple[int, str]:
    tool = str(event.get("tool_name") or "")
    if not tool:
        return DENY, "どの道具を使うのか読み取れませんでした"
    server = mcp_server(tool)
    if "notion" in server.lower() and server != GATEWAY_SERVER:
        return DENY, ("研究の Notion は research-notion のゲートウェイだけを使います"
                      "（ほかの Notion 連携は研究ホームの外まで届きます）")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return ALLOW, ""
    for key in COMMAND_KEYS:
        if raw_token(str(tool_input.get(key) or "")):
            return DENY, ("生の NOTION_TOKEN は使えません"
                          "（Notion は research-notion のゲートウェイから操作してください）")
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
