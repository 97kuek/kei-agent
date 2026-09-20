"""早稲田の時限と時刻の対応。

朝のまとめで、授業・会議・締切を1本の時系列に並べるために使う。
Notion の「授業」には曜日（月〜日・他）と時限（1〜7）が入っているので、ここで時刻に直す。

学部や年度で変わることがあるので、変えるのはこのファイルだけで済むようにしてある。
"""

from __future__ import annotations

from datetime import date, datetime, time

# 早稲田大学の標準の時間割（1時限 90 分）
WASEDA = {
    1: (time(8, 50), time(10, 30)),
    2: (time(10, 40), time(12, 20)),
    3: (time(13, 10), time(14, 50)),
    4: (time(15, 5), time(16, 45)),
    5: (time(17, 0), time(18, 40)),
    6: (time(18, 55), time(20, 35)),
    7: (time(20, 45), time(22, 25)),
}
WEEKDAYS = "月火水木金土日"


def weekday_of(day: date) -> str:
    """その日の曜日（「月」など）。"""
    return WEEKDAYS[day.weekday()]


def span(period: int | None) -> tuple[time, time] | None:
    """時限の始まりと終わり。時限が無い（集中講義など）ときは None。"""
    try:
        return WASEDA[int(period)]
    except (TypeError, ValueError, KeyError):
        return None


def at(day: date, period: int | None) -> tuple[datetime, datetime] | None:
    """その日の、その時限の始まりと終わり。"""
    found = span(period)
    if found is None:
        return None
    return datetime.combine(day, found[0]), datetime.combine(day, found[1])
