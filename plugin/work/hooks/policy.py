#!/usr/bin/env python3
"""仕事エージェントの二の柵（PreToolUse）。

会社の Microsoft 365 は読むだけ。メール送信、Teams 投稿、予定やファイルの作成・更新・削除を断る。
検索・取得・一覧・本文の閲覧は通す。
Microsoft 365 以外の連携（claude.ai のコネクタ）も、書き込み・送信にあたる道具を断る。

断る理由だけを stderr に出し、tool の中身は出さない。
"""

from __future__ import annotations

import json
import sys

ALLOW, DENY = 0, 2

M365 = "mcp__claude_ai_Microsoft_365__"
# 仕事エージェントが使う連携。ほかの連携（Slack・Notion・Box など）への書き込みは断る
OWN_CONNECTORS = frozenset({"Microsoft_365"})
# 担当外の連携（claude.ai のコネクタ）で、名前にこれが入っていたら書き込み・外へ出す操作とみなして断る
OTHER_WRITE_WORDS = ("send", "create", "update", "delete", "modify", "move", "upload", "post", "reply",
                     "forward", "respond", "trash", "rename", "copy", "set_", "add", "remove", "edit",
                     "write", "archive", "schedule", "batch", "draft", "complete", "share", "invite",
                     "publish", "submit", "spawn")
CLAUDE_AI = "mcp__claude_ai_"
# 名前にこれが入っていたら、外へ出す操作とみなす
WRITE_WORDS = ("send", "create", "update", "delete", "modify", "move", "upload", "post",
               "reply", "forward", "respond", "trash", "untrash", "rename", "copy", "set_")


def other_connector_write(tool: str) -> bool:
    """担当外の claude.ai コネクタへの書き込みか。"""
    if not tool.startswith(CLAUDE_AI):
        return False
    connector, _, name = tool[len(CLAUDE_AI):].partition("__")
    return connector not in OWN_CONNECTORS and any(word in name.lower() for word in OTHER_WRITE_WORDS)


def decide(event: dict) -> tuple[int, str]:
    tool = str(event.get("tool_name") or "")
    if not tool:
        return DENY, "どの道具を使うのか読み取れませんでした"
    if tool.startswith(M365):
        name = tool[len(M365):].lower()
        if any(word in name for word in WRITE_WORDS):
            return DENY, ("仕事のツールは読み取り専用です（送信・投稿・予定やファイルの変更はできません）。"
                          "文章案を返すところまでにして、送るのは依頼者に任せてください")
    if other_connector_write(tool):
        return DENY, "仕事エージェントは Microsoft 365 以外の連携に書き込めません"
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
