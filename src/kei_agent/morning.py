"""朝のまとめ（今日の時系列）。

研究・授業・仕事で分けず、**時刻の早い順に1本**へ並べる。授業（Notion の「授業」＋早稲田の時限）、
会議（Outlook）、締切（Moodle）を混ぜる。時刻の無いもの（今週の締切、先行研究の新着）は、
下に一言ずつ添える。

文を組み立てるだけの置き場所（集めるのは schedule.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAYS = "月火水木金土日"
CLASS, MEETING, DUE = "🎓", "💼", "⏰"
NOTHING = "今日は、時間の決まった予定がないよ。"
# 下に添える「このあと」の締切を、いくつまで出すか
MAX_LATER = 3


@dataclass(frozen=True)
class Entry:
    """時系列に並べる1件。"""
    at: datetime
    icon: str
    text: str
    end: datetime | None = None

    @property
    def span(self) -> str:
        if self.end is None:
            return f"{self.at:%H:%M}      "
        return f"{self.at:%H:%M}–{self.end:%H:%M}"


def _at(value: str) -> datetime | None:
    text = (value or "").strip()
    if "." in text:
        head, _, frac = text.partition(".")
        text = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def day_label(day: datetime) -> str:
    return f"{day.month}/{day.day}（{WEEKDAYS[day.weekday()]}）"


def entries(classes: list[dict], events: list[dict], dues: list[dict], now: datetime) -> list[Entry]:
    """今日ぶんだけを、時刻の早い順に並べる。"""
    today = now.date()
    found: list[Entry] = []
    for item in classes or []:
        start, end = _at(item.get("start", "")), _at(item.get("end", ""))
        if start and start.date() == today:
            found.append(Entry(start, CLASS, item.get("subject", ""), end))
    for item in events or []:
        start, end = _at(item.get("start", "")), _at(item.get("end", ""))
        if not start or start.date() != today:
            continue
        where = f"（{item['location']}）" if item.get("location") else ""
        found.append(Entry(start, MEETING, f"{item.get('subject', '')}{where}", end))
    for item in dues or []:
        at = _at(item.get("at", ""))
        if at and at.date() == today:
            course = f"{item['course']} " if item.get("course") else ""
            found.append(Entry(at, DUE, f"締切: {course}{item.get('title', '')}"))
    return sorted(found, key=lambda e: (e.at, e.icon))


def later(dues: list[dict], now: datetime, days: int = 7) -> str:
    """今日より先の締切を1行で。"""
    limit = (now + timedelta(days=days)).date()
    found = []
    for item in dues or []:
        at = _at(item.get("at", ""))
        if at and now.date() < at.date() <= limit:
            found.append((at, item))
    if not found:
        return ""
    found.sort(key=lambda pair: pair[0])
    shown = "、".join(f"{at.month}/{at.day} {item.get('title', '')[:24]}" for at, item in found[:MAX_LATER])
    rest = f"（ほか {len(found) - MAX_LATER} 件）" if len(found) > MAX_LATER else ""
    return f"このあとの締切: {shown}{rest}"


def text(classes: list[dict], events: list[dict], dues: list[dict], now: datetime,
         notes: list[str] | None = None) -> str:
    """朝のまとめの本文（Slack にそのまま出せる形）。"""
    lines = [f"☀️ {day_label(now)} の予定"]
    found = entries(classes, events, dues, now)
    lines += [f"`{entry.span}` {entry.icon} {entry.text}" for entry in found] or [NOTHING]
    rest = [line for line in [later(dues, now), *(notes or [])] if line]
    if rest:
        lines.append("─")
        lines += rest
    return "\n".join(lines)
