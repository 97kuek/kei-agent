"""AI の返事から JSON を拾う（前後に文やコードの囲みが付いていても読む）。

本体（振り分け・分類）も担当も使うので、a2a-sdk などの重いものは読み込まない。
"""

from __future__ import annotations

import json


def _values(text: str, opener: str) -> list[object]:
    """文の中にある、`opener`（`[` か `{`）で始まる JSON を、外側のものだけ前から順に。

    各位置から1度だけ読み、読めた値の中は飛ばす（全部の括弧の組を試さない）。
    """
    decoder = json.JSONDecoder()
    found: list[object] = []
    position = text.find(opener)
    while position >= 0:
        try:
            value, end = decoder.raw_decode(text, position)
        except ValueError:
            position = text.find(opener, position + 1)
            continue
        found.append(value)
        position = text.find(opener, end)
    return found


def json_list(text: str) -> list[dict]:
    """JSON の配列で答えさせたときの、返事の読み取り。

    「接続が拒否されました」のような文を「予定0件」と取り違えないよう、JSON の配列が
    見つからなければ断る（ValueError）。前置きに角括弧があっても、読めるものを探す。

    後ろに出典（`[1, 2]`）のような別の配列が付くことがあるので、**中身のある配列を優先**する。
    中身のある配列が複数あるときは、後ろのもの（答えの本体は最後に来る）。
    """
    text = text or ""
    lists = [value for value in _values(text, "[") if isinstance(value, list)]
    for found in reversed(lists):
        items = [item for item in found if isinstance(item, dict)]
        if items:
            return items
    if lists:
        return []
    raise ValueError(f"返事を読めません（JSON の配列がありません）: {text[:200]}")


def json_object(text: str, key: str) -> dict:
    """返事から、`key` を持つ JSON オブジェクトを拾う。無ければ ValueError。"""
    for value in _values(text or "", "{"):
        if isinstance(value, dict) and key in value:
            return value
    raise ValueError("JSON オブジェクトがありません")
