"""Moodle のカレンダー書き出し（ics）を読む。

Moodle のウェブサービス（API）が使えないので、カレンダーの書き出し URL（`authtoken` 付き）から
締切を読む。ics は行の折り返しと記号のエスケープだけ気をつければ、標準ライブラリで読める。

仕様（RFC 5545）のうち、ここで使うのは VEVENT と、その中の SUMMARY / DTSTART / DESCRIPTION / URL /
CATEGORIES / UID だけ。繰り返しの予定（RRULE）は課題の締切には出てこないので扱わない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

# Moodle が締切に付ける言い回し。同じ活動が「開始」と「終了」で2件入るので、終わりのほうだけを締切として扱う
# 末尾が締切の言い回しなら、名前の途中に「開始」があっても締切（「開始前アンケート の 終了」）
_DUE_SUFFIX = re.compile(r"(提出期限|終了|終了日時|締切|締め切り|期限)\s*$")
_DUE_WORDS = re.compile(r"終了|提出期限|締切|締め切り|期限|\b(due|closes?|ends?)\b", re.IGNORECASE)
_START_WORDS = re.compile(r"開始|オープン|\b(opens?|starts?)\b", re.IGNORECASE)
# 行の折り返し（CRLF か LF のあとに空白1つ）
_FOLD = re.compile(rb"\r?\n[ \t]")
# 科目名の末尾に付く履修コード（`…(2019ZZ2600000126)`）
_COURSE_CODE = re.compile(r"\s*[（(][0-9A-Z]{6,}[）)]\s*$")
_UNESCAPE = {"\\n": "\n", "\\N": "\n", "\\,": ",", "\\;": ";", "\\\\": "\\"}
_ESCAPED = re.compile(r"\\[nN,;\\]")


@dataclass(frozen=True)
class Event:
    """ics の1件。課題の締切として使うぶんだけ持つ。"""
    uid: str
    summary: str
    starts_at: datetime | None
    course: str = ""
    description: str = ""
    url: str = ""

    @property
    def kind(self) -> str:
        """`due`（締切）／`start`（受付や公開の開始）／`other`（それ以外）。"""
        if _DUE_SUFFIX.search(self.summary):
            return "due"
        if _START_WORDS.search(self.summary):
            return "start"
        if _DUE_WORDS.search(self.summary):
            return "due"
        return "other"

    @property
    def is_due(self) -> bool:
        return self.kind == "due"

    @property
    def course_name(self) -> str:
        """科目名（末尾の履修コードを外したもの）。"""
        return _COURSE_CODE.sub("", self.course).strip()


def unfold_bytes(data: bytes) -> bytes:
    """折り返しを、文字に直す前に外す（折り返しは75バイトごとなので、日本語の1文字が途中で切れる）。"""
    return _FOLD.sub(b"", data)


def unfold(text: str) -> list[str]:
    """折り返された行（次の行が空白で始まる）を1行に戻す。"""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _value(line: str) -> tuple[str, str, dict[str, str]]:
    """`DTSTART;TZID=Asia/Tokyo:20260925T235900` を (名前, 値, パラメータ) に分ける。"""
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    options = dict(p.split("=", 1) for p in params if "=" in p)
    return name.upper(), _ESCAPED.sub(lambda m: _UNESCAPE[m.group(0)], value), options


def _time(value: str, params: dict[str, str]) -> datetime | None:
    """ics の日時。`Z` は UTC、それ以外は手元の時刻として読む。日付だけなら、その日の終わりにする。"""
    value = value.strip()
    try:
        if value.endswith("Z"):
            # Moodle は UTC で書き出す。手元の時刻に直してから使う
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).astimezone().replace(tzinfo=None)
        if params.get("VALUE") == "DATE" or len(value) == 8:
            day = datetime.strptime(value, "%Y%m%d")
            return day.replace(hour=23, minute=59)
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def parse(text: str) -> list[Event]:
    """ics の中身から、予定を取り出す。"""
    events: list[Event] = []
    current: dict | None = None
    for line in unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if line.startswith("END:VEVENT"):
            if current is not None:
                events.append(Event(
                    uid=current.get("UID", ""),
                    summary=current.get("SUMMARY", ""),
                    starts_at=current.get("DTSTART"),
                    course=current.get("CATEGORIES", ""),
                    description=current.get("DESCRIPTION", ""),
                    url=current.get("URL", ""),
                ))
            current = None
            continue
        if current is None or ":" not in line:
            continue
        name, value, params = _value(line)
        if name == "DTSTART":
            current["DTSTART"] = _time(value, params)
        elif name in ("UID", "SUMMARY", "DESCRIPTION", "URL", "CATEGORIES"):
            current[name] = value
    return events


def due_events(text: str, since: date | None = None, days: int = 90) -> list[Event]:
    """締切らしい予定を、近い順に。過ぎたものと、先すぎるものは落とす。

    since を省くと今から数える（今日の朝に過ぎた締切は入れない）。
    """
    day_start = datetime.combine(since or date.today(), datetime.min.time())
    end = day_start + timedelta(days=days)
    start = day_start if since is not None else max(day_start, datetime.now())
    found = [e for e in parse(text)
             if e.is_due and e.starts_at is not None and start <= e.starts_at <= end]
    return sorted(found, key=lambda e: e.starts_at)
