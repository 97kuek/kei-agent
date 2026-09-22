#!/usr/bin/env python3
"""仕事エージェントの二の柵（PreToolUse）。

会社の Microsoft 365 は読むだけ。メール送信、Teams 投稿、予定やファイルの作成・更新・削除を断る。
検索・取得・一覧・本文の閲覧は通す。

断る理由だけを stderr に出し、tool の中身は出さない。
"""

from __future__ import annotations

import json
import sys

ALLOW, DENY = 0, 2

M365 = "mcp__claude_ai_Microsoft_365__"
# 名前にこれが入っていたら、外へ出す操作とみなす
WRITE_WORDS = ("send", "create", "update", "delete", "modify", "move", "upload", "post",
               "reply", "forward", "respond", "trash", "untrash", "rename", "copy", "set_")


def decide(event: dict) -> tuple[int, str]:
    tool = str(event.get("tool_name") or "")
    if not tool:
        return DENY, "どの道具を使うのか読み取れませんでした"
    if tool.startswith(M365):
        name = tool[len(M365):].lower()
        if any(word in name for word in WRITE_WORDS):
            return DENY, ("仕事のツールは読み取り専用です（送信・投稿・予定やファイルの変更はできません）。"
                          "文章案を返すところまでにして、送るのは依頼者に任せてください")
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
