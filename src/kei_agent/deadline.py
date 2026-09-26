"""締切の読み方（Slack・声・Daily の材料で共通）。

Moodle の締切は「月曜 0:00」の形が多い。人はこれを「日曜の夜 24:00」と読むので、表示と日付の振り分けは
0:00 ちょうどを前の日の 24:00 として扱う（締切の時刻そのものは変えない）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def _midnight(at: datetime) -> bool:
    return at.hour == 0 and at.minute == 0


def day(at: datetime) -> date:
    """締切の日。0:00 ちょうどは前の日の終わり。"""
    return (at - timedelta(days=1)).date() if _midnight(at) else at.date()


def clock(at: datetime) -> str:
    """締切の時刻。0:00 ちょうどは 24:00 と書く。"""
    return "24:00" if _midnight(at) else f"{at:%H:%M}"
