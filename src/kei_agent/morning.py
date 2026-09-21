"""朝のまとめ（今日の時系列）。

研究・授業・仕事で分けず、**時刻の早い順に1本**へ並べる。授業（Notion の「授業」＋早稲田の時限）、
会議（Outlook）、締切（Moodle）を混ぜる。時刻の無いもの（今週の締切、先行研究の新着）は、
下に一言ずつ添える。

文を組み立てるだけの置き場所（集めるのは schedule.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from kei_agent.slack_text import escape

WEEKDAYS = "月火水木金土日"
CLASS, MEETING, DUE = "🎓", "💼", "⏰"
NOTHING = "今日は、時間の決まった予定がないよ。"
# 下に添える「このあと」の締切を、いくつまで出すか
MAX_LATER = 3

# 1日の帯（今日の形をひと目で見るためのもの）。
# スマホでも崩れないよう、**1行だけ**にして、行をまたぐ桁合わせをしない。中身は ASCII だけにする
# （`▓` や `█` は幅の扱いがフォントで割れるので使わない）。細かい時刻は下の一覧と「空き」の行で読む。
BAND_FROM, BAND_TO = 9, 21          # ふだん見る時間帯。予定がはみ出したら、その分だけ広げる
BUSY, DUE_MARK, FREE = "#", "!", "." # 授業・会議 / 締切 / 空き
# これより短い切れ間は「空き」に数えない（移動と片付けで消える）
MIN_FREE_MINUTES = 30
MAX_FREE_SHOWN = 3


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
            found.append(Entry(start, CLASS, escape(item.get("subject", "")), end))
    for item in events or []:
        start, end = _at(item.get("start", "")), _at(item.get("end", ""))
        if not start or start.date() != today:
            continue
        where = f"（{escape(item['location'])}）" if item.get("location") else ""
        found.append(Entry(start, MEETING, f"{escape(item.get('subject', ''))}{where}", end))
    for item in dues or []:
        at = _at(item.get("at", ""))
        if at and at.date() == today:
            course = f"{escape(item['course'])} " if item.get("course") else ""
            found.append(Entry(at, DUE, f"締切: {course}{escape(item.get('title', ''))}"))
    return sorted(found, key=lambda e: (e.at, e.icon))


def _hours(found: list[Entry]) -> tuple[int, int]:
    """帯に出す時間の範囲（開始の時、終わりの時）。はみ出す予定があれば、その分だけ広げる。"""
    start, end = BAND_FROM, BAND_TO
    for entry in found:
        start = min(start, entry.at.hour)
        last = entry.end or entry.at
        # ちょうどの時刻で終わるものは、その時間を埋めない（10:00 終わりは 9 時台まで）
        end = max(end, last.hour + (1 if last.minute else 0))
    return start, max(end, start + 1)


def band(found: list[Entry], now: datetime | None = None) -> str:
    """今日の形を表す1行。1文字が1時間で、埋まっている時間を `#`、締切のある時間を `!` にする。"""
    if not found:
        return ""
    start, end = _hours(found)
    cells = [FREE] * (end - start)
    for entry in found:
        if entry.end is None:
            hour = entry.at.hour - start
            if 0 <= hour < len(cells) and cells[hour] == FREE:
                cells[hour] = DUE_MARK
            continue
        # 予定が少しでもかかる時間は、埋まっているものとして扱う
        first = entry.at.hour - start
        last = (entry.end - timedelta(minutes=1)).hour - start
        for hour in range(max(first, 0), min(last, len(cells) - 1) + 1):
            cells[hour] = BUSY
    return f"`{start}時 {''.join(cells)} {end}時`"


def free_slots(found: list[Entry], now: datetime) -> str:
    """予定と予定のあいだの、まとまった空き時間。"""
    spans = sorted((e.at, e.end) for e in found if e.end is not None)
    if not spans:
        return ""
    start, end = _hours(found)
    day = now.date()
    edge_from = datetime.combine(day, datetime.min.time()).replace(hour=start)
    edge_to = datetime.combine(day, datetime.min.time()).replace(hour=min(end, 23))
    gaps, cursor = [], edge_from
    for at, until in [*spans, (edge_to, edge_to)]:
        if (at - cursor).total_seconds() >= MIN_FREE_MINUTES * 60:
            gaps.append((cursor, at))
        cursor = max(cursor, until)
    if not gaps:
        return ""
    # 使えるのは長い切れ間なので、長い順に選んでから、時刻の順に並べ直して見せる
    picked = sorted(sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[:MAX_FREE_SHOWN])
    shown = "、".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in picked)
    rest = f"（ほか {len(gaps) - len(picked)} か所）" if len(gaps) > len(picked) else ""
    return f"空き: {shown}{rest}"


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
    shown = "、".join(f"{at.month}/{at.day} {escape(item.get('title', ''))[:24]}" for at, item in found[:MAX_LATER])
    rest = f"（ほか {len(found) - MAX_LATER} 件）" if len(found) > MAX_LATER else ""
    return f"このあとの締切: {shown}{rest}"


def text(classes: list[dict], events: list[dict], dues: list[dict], now: datetime,
         notes: list[str] | None = None) -> str:
    """朝のまとめの本文（Slack にそのまま出せる形）。"""
    lines = [f"☀️ {day_label(now)} の予定"]
    found = entries(classes, events, dues, now)
    if found:
        lines.append(band(found, now))
        lines += [f"`{entry.span}` {entry.icon} {entry.text}" for entry in found]
    else:
        lines.append(NOTHING)
    rest = [line for line in [free_slots(found, now), later(dues, now), *(notes or [])] if line]
    if rest:
        lines.append("─")
        lines += rest
    return "\n".join(lines)
