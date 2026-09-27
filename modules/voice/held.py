"""本体が押してきたもの（朝の予定、動いている依頼の数、上限）を、手元に置いておく入れ物の世話。

聞かれてから取りに行かず、押されてきたものを道具（tools.py）がすぐ読む（docs/architecture.md の「声のレイヤ」）。
"""

from __future__ import annotations

from datetime import datetime


def current(held: dict, now: datetime | None = None) -> dict:
    """古くなったものを捨てて返す。終わった数は**その日のぶん**、上限はやり直しの時刻まで。"""
    now = now or datetime.now()
    today = now.date().isoformat()
    until = _reset_at(str(held.get("limited_until") or ""))
    if held.get("day") != today:
        if held.get("day") and until is None:
            # やり直しの時刻が読めなかった上限は、日が変わったら忘れる
            held.pop("limited", None)
        held.pop("done", None)
        held.pop("failed", None)
        held["day"] = today
    if held.get("limited") and until is not None and _passed(until, now):
        held.pop("limited", None)
        held.pop("limited_until", None)
    return held


def _reset_at(at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(at)
    except ValueError:
        return None


def _passed(reset: datetime, now: datetime) -> bool:
    if reset.tzinfo is not None:
        now = now.astimezone() if now.tzinfo is None else now
    elif now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return now >= reset
