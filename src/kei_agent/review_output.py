"""Retro & Planning の利用者向け Slack 出力を検証する。"""

from __future__ import annotations


HEADINGS = ("*今日の成果*", "*未完了タスク*")
NIGHT_QUESTION = "夜間に実行したいタスクはありますか？"


class ReviewOutputError(ValueError):
    """モデルの最終文が Retro の投稿契約を満たさない。"""


def _invalid() -> ReviewOutputError:
    return ReviewOutputError("Retro の返答が指定形式ではありません")


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
    if any(line.startswith("#") or "Codex App" in line or line.startswith("/Users/") for line in lines):
        raise _invalid()
    return normalized


def review_footer(notion_url: str | None, title: str) -> str:
    """内部ファイルを出さず、結論を書き込む場所だけを案内する。"""
    if notion_url:
        return (f"振り返りの結論があれば、このスレッドか Notion の <{notion_url}|{title}> に書いてね。"
                "明日の Daily に反映するよ。")
    return "振り返りの結論があれば、このスレッドに書いてね。明日の Daily に反映するよ。"
