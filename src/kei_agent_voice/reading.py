"""読み上げのために、文を整えて切り分ける。

Codex の返事は目で読む形で来る（記号、箇条書き、長い一文）。声にすると邪魔なものを落とし、
文の切れ目で分ける。

分けるのは、**長い返事の出だしを早く鳴らすため**でもある。1文ずつ合成して順に鳴らせば、
全部の合成を待たずに喋り始められる（AivisSpeech は一度に 500 文字程度までという制限もある）。
"""

from __future__ import annotations

import re

# 読み上げの1回ぶんの上限。長すぎると止めにくく、短すぎると切れ切れに聞こえる
MAX_SENTENCE = 120
_SPLIT = re.compile(r"(?<=[。．！？!?])")
# 声に出すと邪魔になる記号（コードの引用、強調、見出し、表）
_STRIP = re.compile(r"[`*_#>|]+")
# 行頭の箇条書きの印
_BULLET = re.compile(r"^\s*(?:[-*・•]|\d+[.)])\s*")


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
