"""呼びかけ（「けい」）を、文字起こしの結果から見つける。

専用のウェイクワードのエンジンは使わない。文字起こしを常時動かし、出てきた文字を見るだけ
（docs/voice.md の9節）。

実測で分かった揺れ:

- **カタカナの「ケイ」は一度も出ない。** 毎回ひらがな
- **漢字に化ける。** 「けい」→「経」。同音（軽・敬・圭・京・計）も同類
- **先頭が落ちる。** 「けい」→「け」だけになった回があった
- **長音が落ちる。** 「おーい」→「おい」

いっぽう、心配していた誤爆は起きなかった（`今日` は「きょう」なので当たらない）。
"""

from __future__ import annotations

import re

# 呼びかけとして認める形。ひらがなに寄せてから見る。
# 「けい」は2文字あって特徴があるので、頭のほうに出ればよい
SPELLED = ("けい", "けー")
# 漢字1文字の同音は、ふつうの言葉にも出てくる（「経過」「計測」「軽い」）。
# **文の先頭に来たときだけ**呼びかけとみなす（「この論文の経過を」で誤爆しないため）
KANJI = ("経", "軽", "敬", "圭", "京", "計", "契")
# 「けい」を探す範囲。呼びかけは文の頭にある（「ねえけい」「おいけい」のような前置きを許す）
HEAD = 6
# 呼びかけのあとに続く区切り（読点まで含めて落とす）
_LEAD = re.compile(r"^[\s、。!?！？]+")


def _hiragana(text: str) -> str:
    """カタカナをひらがなに寄せ、空白と記号を落とす。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:            # カタカナ → ひらがな
            out.append(chr(code - 0x60))
        elif ch.isspace() or ch in "、。,.!?！？「」『』・":
            continue
        else:
            out.append(ch)
    return "".join(out)


def called(text: str) -> bool:
    """呼びかけられたか。

    「けい」は文の頭のほうに出ればよい。漢字1文字は、文の先頭に来たときだけ。
    """
    plain = _hiragana(text)
    if any(name in plain[:HEAD] for name in SPELLED):
        return True
    return plain.startswith(KANJI)


def request(text: str) -> str:
    """呼びかけを落として、依頼の本文だけを返す。

    元の文字（漢字やカタカナのまま）から落とすので、`_hiragana` は判定にだけ使う。
    """
    if not called(text):
        return text.strip()
    body = text.strip()
    cut = 0
    for name in (*SPELLED, "ケイ", "ケー", *KANJI):
        found = body.find(name, 0, HEAD + len(name))
        if found >= 0:
            cut = max(cut, found + len(name))
    return _LEAD.sub("", body[cut:]).strip()
