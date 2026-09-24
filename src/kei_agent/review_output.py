"""Retro & Planning の出力契約の互換 API。"""

from __future__ import annotations

from kei_agent.response_output import OutputError, validate_review

ReviewOutputError = OutputError


def validate_review_reply(text: str) -> str:
    """後方互換のため残した Retro 検証関数。"""
    return validate_review(text)


def review_footer(notion_url: str | None, title: str) -> str:
    """内部ファイルを出さず、結論を書き込む場所だけを案内する。"""
    if notion_url:
        return (f"振り返りの結論があれば、このスレッドか Notion の <{notion_url}|{title}> に書いてね。"
                "明日の Daily に反映するよ。")
    return "振り返りの結論があれば、このスレッドに書いてね。明日の Daily に反映するよ。"
