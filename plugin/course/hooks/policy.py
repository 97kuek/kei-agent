#!/usr/bin/env python3
"""大学エージェントの二の柵（PreToolUse）。

Box は読むだけ。アップロード・作成・移動・複製・更新・コメントを断る。
Notion は断らない。授業ホームの外へは、専用プロファイルと Notion 側の共有設定で届かない。

断る理由だけを stderr に出し、tool の中身は出さない。
"""

from __future__ import annotations

import json
import sys

ALLOW, DENY = 0, 2

BOX = "mcp__claude_ai_Box__"
# Box で通す道具（読むだけ）。ここに無い Box の道具は、名前を問わず断る
BOX_READS = frozenset({
    "search_files_keyword", "search_folders_by_name", "search_files_metadata",
    "list_folder_content_by_folder_id", "list_file_comments", "list_item_collaborations",
    "list_metadata_templates", "list_tasks", "list_hubs", "get_hub_details", "get_hub_items",
    "get_file_details", "get_file_content", "get_file_preview", "get_preview_page",
    "get_folder_details", "get_metadata_template_schema", "who_am_i",
})


def decide(event: dict) -> tuple[int, str]:
    tool = str(event.get("tool_name") or "")
    if not tool:
        return DENY, "どの道具を使うのか読み取れませんでした"
    if tool.startswith(BOX) and tool[len(BOX):] not in BOX_READS:
        return DENY, ("Box は読み取り専用です（アップロード・作成・移動・複製・更新はできません）。"
                      "残すものは Notion の授業ホームに置いてください")
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
