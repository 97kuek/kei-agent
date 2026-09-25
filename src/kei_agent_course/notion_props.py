"""授業ホームの Notion プロパティを読む・組み立てる小さな道具。"""

from __future__ import annotations


def plain(prop: dict | None) -> str:
    """title と rich_text の中身を、ただの文字列にする（応答の plain_text と書き込み用の text の両方を読む）。"""
    parts = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(str(part.get("plain_text") or (part.get("text") or {}).get("content") or "")
                   for part in parts).strip()


def select(prop: dict | None) -> str:
    return ((prop or {}).get("select") or {}).get("name") or ""


def number(prop: dict | None) -> float | int | None:
    return (prop or {}).get("number")


def title(value: str) -> dict:
    return {"title": [{"text": {"content": value}}]}


def text(value: str) -> dict:
    """rich_text。空文字は空の配列にする（Notion は空の text を受け付けない）。"""
    return {"rich_text": [{"text": {"content": value}}]} if value else {"rich_text": []}
