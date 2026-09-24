"""Slack に表示してよい Kei Agent の最終出力だけを扱う。"""

from __future__ import annotations

import re

FINAL_OPEN = "<<kei-agent-final>>"
FINAL_CLOSE = "<<kei-agent-final-end>>"

DAILY_HEADINGS = (
    "**今日のタスク**",
    "**夜間処理の結果**",
    "**確認待ち・期日・止まっているテーマ・返事待ち**",
    "**今日考えるとよい問い**",
)
REVIEW_HEADINGS = ("**今日の成果**", "**未完了タスク**")
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
_INTERNAL_EXCEPTION = re.compile(r"\b(?:Traceback|[A-Za-z][\w.]*(?:Error|Exception))\b")


class OutputError(ValueError):
    """モデルの文が Slack 出力契約を満たさない。"""


def _contains_local_path(text: str) -> bool:
    """Web URL は残し、ローカルの絶対・相対パスだけを拒否する。"""
    local_candidate = _WEB_URL.sub("", text)
    return bool(_LOCAL_PATH.search(local_candidate) or _RELATIVE_LOCAL_PATH.search(local_candidate))


def _forbidden(text: str) -> bool:
    return bool(_contains_local_path(text) or _INTERNAL_PROGRESS.search(text))


def finalize_conversation(text: str) -> str:
    """final marker 内だけを利用者向け本文として採用する。"""
    normalized = text.strip().replace("\r\n", "\n")
    if normalized.count(FINAL_OPEN) != 1 or normalized.count(FINAL_CLOSE) != 1:
        raise OutputError("conversation contract")
    _before, marked = normalized.split(FINAL_OPEN, 1)
    body, close, tail = marked.partition(FINAL_CLOSE)
    if not close or tail.strip() or not body.strip() or _forbidden(body):
        raise OutputError("conversation contract")
    return body.strip()


def validate_structured_response(text: str) -> str:
    """定型 A2A の、コードで組み立てた返答を Slack に出せる形か確かめる。

    自由文のモデル応答は ``finalize_conversation`` を必ず通す。一方、締切や
    同期結果のような定型処理は A2A の ``data`` と固定の整形で表示するため、
    marker を要求せず、内部経過・例外・パスだけを拒否する。
    """
    normalized = text.strip().replace("\r\n", "\n")
    if (not normalized or FINAL_OPEN in normalized or FINAL_CLOSE in normalized
            or _forbidden(normalized) or _INTERNAL_EXCEPTION.search(normalized)):
        raise OutputError("structured response contract")
    return normalized


def _validate_sections(text: str, headings: tuple[str, ...], message: str) -> str:
    normalized = text.strip().replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    if len(parts) != len(headings):
        raise OutputError(message)
    for part, heading in zip(parts, headings, strict=True):
        prefix = heading + "\n"
        if not part.startswith(prefix) or not part[len(prefix):].strip():
            raise OutputError(message)
    if any(line.startswith("#") for line in normalized.splitlines()) or _forbidden(normalized):
        raise OutputError(message)
    return normalized


def validate_daily(text: str) -> str:
    """Daily の四つの太字 section だけを受け入れる。"""
    return _validate_sections(text, DAILY_HEADINGS, "Daily の返答が指定形式ではありません")


def validate_review(text: str) -> str:
    """Retro の二つの section と夜間質問だけを受け入れる。"""
    normalized = text.strip().replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    if len(parts) != 3 or parts[-1] != NIGHT_QUESTION:
        raise OutputError("Retro の返答が指定形式ではありません")
    return _validate_sections(
        "\n\n".join(parts[:2]), REVIEW_HEADINGS, "Retro の返答が指定形式ではありません"
    ) + "\n\n" + NIGHT_QUESTION


def safe_failure(kind: str = "conversation") -> str:
    """内部詳細を含めない、Slack 用の固定失敗文を返す。"""
    messages = {
        "conversation": "⚠️ 返答を利用者向けの形に整えられなかったよ。もう一度頼んでね。",
        "daily": "⚠️ Daily を利用者向けの形に整えられなかったよ。あとでもう一度実行するね。",
        "review": "⚠️ 振り返りを利用者向けの形に整えられなかったよ。もう一度頼んでね。",
        "connection": "⚠️ 接続に失敗したよ。少し時間を置いてもう一度頼んでね。",
        "timeout": "⚠️ 時間がかかりすぎたよ。少し時間を置いてもう一度頼んでね。",
    }
    return messages.get(kind, messages["conversation"])
