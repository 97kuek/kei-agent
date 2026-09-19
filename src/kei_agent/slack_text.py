"""Slack に出す文字の扱い: 印の絵文字、Claude が返答に書く合図、メンションの除去、長い文の分割。"""

from __future__ import annotations

import re

PROGRESS_PREFIX = "⏳"
DONE_PREFIX = "✅"
FAILED_PREFIX = "⚠️"

# 依頼のメッセージにつけるリアクション（受け取った、答えた、止まった）と、夜間の Task にする印
SEEN_REACTION = "eyes"
DONE_REACTION = "white_check_mark"
FAILED_REACTION = "warning"
NIGHT_REACTION = "crescent_moon"

# Claude が依頼者の判断を待つときに、返答の最後の行をこれで始める（prompts/system.md）
AWAITING_MARKER = "❓ 確認:"
# 作業がひと段落して、新しいスレッドで続けたほうがよいときに、返答の最後の行をこれで始める（handoff.py）
HANDOFF_MARKER = "🧵 区切り:"

SLACK_TEXT_LIMIT = 11000

_MENTION = re.compile(r"<@[A-Z0-9]+>")


def clean_text(text: str) -> str:
    return _MENTION.sub("", text or "").strip()


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes}分"


def split_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    """Slack の文字数上限に合わせて、なるべく段落の切れ目で分ける。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks or [""]


def message_text(message: dict) -> str:
    """メッセージの本文。`markdown_text` や流して見せた返事は text が空で blocks に入る。"""
    text = message.get("text") or ""
    if text:
        return text
    parts = []
    for block in message.get("blocks") or []:
        if block.get("type") == "markdown" and block.get("text"):
            parts.append(block["text"])
        for element in block.get("elements") or []:
            for item in element.get("elements") or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
    return "\n".join(parts)
