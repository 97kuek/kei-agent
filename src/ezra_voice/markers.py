"""Codex の返事に混ぜてもらう目印の行と、読み上げのための文の切り出し。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Slack 側（🛠 着手 / 📦 取り込み / 🔒 接続:）と同じやり方で揃えている
DECISION = "📌 決定:"
REQUEST = "🛠 依頼:"
THEME = "🎯 テーマ:"

# 読み上げの1回ぶんの上限。長すぎると止めにくく、短すぎると切れ切れに聞こえる
MAX_SENTENCE = 120
_SPLIT = re.compile(r"(?<=[。．！？!?])")
# 声に出すと邪魔になる記号（コードの引用、強調、見出し、表）
_STRIP = re.compile(r"[`*_#>|]+")
# 行頭の箇条書きの印
_BULLET = re.compile(r"^\s*(?:[-*・•]|\d+[.)])\s*")


@dataclass
class Reply:
    """Codex の返事を、読み上げる文と、目印の中身に分けたもの。"""
    spoken: str = ""
    decisions: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    theme: str | None = None


def parse(text: str) -> Reply:
    reply = Reply()
    spoken_lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(DECISION):
            value = line[len(DECISION):].strip()
            if value:
                reply.decisions.append(value)
        elif line.startswith(REQUEST):
            value = line[len(REQUEST):].strip()
            if value:
                reply.requests.append(value)
        elif line.startswith(THEME):
            value = line[len(THEME):].strip().lstrip("#")
            if value:
                reply.theme = value
        else:
            spoken_lines.append(raw)
    reply.spoken = "\n".join(spoken_lines).strip()
    return reply


def for_speech(text: str) -> str:
    """読み上げ用に、声にすると邪魔な記号を落とす。"""
    return _STRIP.sub("", _BULLET.sub("", text)).strip()


def sentences(text: str) -> list[str]:
    """文の切れ目で分ける。長い文は、読みやすい長さに割る。"""
    out: list[str] = []
    for block in text.splitlines():
        block = for_speech(block)
        if not block:
            continue
        for piece in _SPLIT.split(block):
            piece = piece.strip()
            while len(piece) > MAX_SENTENCE:
                cut = piece.rfind("、", 0, MAX_SENTENCE)
                cut = cut + 1 if cut > MAX_SENTENCE // 3 else MAX_SENTENCE
                out.append(piece[:cut])
                piece = piece[cut:].strip()
            if piece:
                out.append(piece)
    return out
