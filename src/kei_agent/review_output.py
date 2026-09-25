"""Retro & Planning の出力契約の互換 API。"""

from __future__ import annotations

from kei_agent.response_output import OutputError, validate_review

ReviewOutputError = OutputError


def validate_review_reply(text: str) -> str:
    """後方互換のため残した Retro 検証関数。"""
    return validate_review(text)
