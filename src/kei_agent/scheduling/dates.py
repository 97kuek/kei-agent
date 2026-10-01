"""日付と時刻の小さな道具。曜日、「9/25（金）」の形、ほかの担当や API から来た時刻の読み取り。"""

from __future__ import annotations

from datetime import date, datetime

WEEKDAYS = "月火水木金土日"


def weekday(day: date) -> str:
    """曜日の1文字（「月」など）。datetime も渡せる。"""
    return WEEKDAYS[day.weekday()]


def day_label(day: date) -> str:
    """9/25（金）の形。datetime も渡せる。"""
    return f"{day.month}/{day.day}（{weekday(day)}）"


def parse_time(value: object) -> datetime | None:
    """ISO 形式の時刻を、タイムゾーンを外して読む。読めなければ None。

    Outlook（Graph）の7桁の小数秒も、Python 3.11 からはそのまま読める。
    """
    try:
        return datetime.fromisoformat(str(value or "").strip()).replace(tzinfo=None)
    except ValueError:
        return None
