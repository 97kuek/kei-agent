"""Retro & Planning の利用者向け Slack 出力を検証する。"""

from __future__ import annotations

import re

HEADINGS = ("*今日の成果*", "*未完了タスク*")
NIGHT_QUESTION = "夜間に実行したいタスクはありますか？"
_LOCAL_PATH = re.compile(r"(?:file://\S+|~/(?:\S+)|(?:^|[\s([{\"])\/(?:\S+))")
_WEB_URL = re.compile(r"https?://\S+")
_RELATIVE_LOCAL_PATH = re.compile(
    r"(?:\b(?:reviews|overview|outputs|inputs|logs|\.kei-agent|\.config|\.local)/\S+"
    r"|\b(?:[\w.-]+/)+[\w.-]+\.(?:md|txt|json|csv|py|html|pdf|xlsx?|docx?))"
)
_INTERNAL_PROGRESS = re.compile(
    r"\b(?:Bash|Read|Glob|Grep|WebSearch|WebFetch|Skill|Codex App|Claude Code|apply_patch|pytest|ruff)\b"
    r"|(?:材料|参照スレッド|スレッド).{0,24}(?:確認|読[みむ])"
    r"|(?:まず|次に|最後に).{0,32}(?:確認|調査|作成|実装|実行|進め)"
    r"|(?:調査|作業|処理).{0,12}(?:中です|します|を開始)",
    re.IGNORECASE,
)


class ReviewOutputError(ValueError):
    """モデルの最終文が Retro の投稿契約を満たさない。"""


def _invalid() -> ReviewOutputError:
    return ReviewOutputError("Retro の返答が指定形式ではありません")


def _contains_local_path(text: str) -> bool:
    """通常の Web URL を残し、ローカルの絶対・相対パスだけを拒否する。"""
    local_candidate = _WEB_URL.sub("", text)
    return bool(_LOCAL_PATH.search(local_candidate) or _RELATIVE_LOCAL_PATH.search(local_candidate))


def validate_review_reply(text: str) -> str:
    """指定された三つの塊だけからなる最終文を受け入れる。

    部分抽出はしない。作業実況を先頭や末尾に付けた出力をそのまま Slack に流さないため、
    1文字でも余分なら失敗として呼び出し元へ返す。
    """
    normalized = text.strip().replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    if len(parts) != 3 or parts[2] != NIGHT_QUESTION:
        raise _invalid()
    if not parts[0].startswith(HEADINGS[0] + "\n") or not parts[1].startswith(HEADINGS[1] + "\n"):
        raise _invalid()
    content = (parts[0][len(HEADINGS[0]) + 1:], parts[1][len(HEADINGS[1]) + 1:])
    if any(not value.strip() for value in content):
        raise _invalid()
    lines = normalized.splitlines()
    if any(line.startswith("#") for line in lines) or _contains_local_path(normalized) or _INTERNAL_PROGRESS.search(normalized):
        raise _invalid()
    return normalized


def review_footer(notion_url: str | None, title: str) -> str:
    """内部ファイルを出さず、結論を書き込む場所だけを案内する。"""
    if notion_url:
        return (f"振り返りの結論があれば、このスレッドか Notion の <{notion_url}|{title}> に書いてね。"
                "明日の Daily に反映するよ。")
    return "振り返りの結論があれば、このスレッドに書いてね。明日の Daily に反映するよ。"
