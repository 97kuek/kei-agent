"""Moodle から締切を取ってくる。

ウェブサービス（API）は使えるものの、トークンを発行する画面がなかったので、
カレンダーの書き出し URL（`authtoken` 付きで、ログインなしで読める）を使う。
URL は環境変数 `MOODLE_ICS_URL` に置く。
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from datetime import date

from kei_agent_course.ics import Event, due_events, parse, unfold_bytes

log = logging.getLogger(__name__)

ICS_ENV = "MOODLE_ICS_URL"
TIMEOUT_SECONDS = 30
# 取り込む先の期間（日）。学期の終わり（2月の締切）まで届く長さにする
WINDOW_DAYS = 180


class MoodleError(RuntimeError):
    pass


def ics_url(env: dict[str, str] | None = None) -> str:
    return (dict(os.environ) if env is None else env).get(ICS_ENV, "")


def fetch(url: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """カレンダーの書き出しを読む。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = unfold_bytes(resp.read()).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        raise MoodleError(f"カレンダーを読めません: {e}") from None
    if "BEGIN:VCALENDAR" not in text:
        raise MoodleError("カレンダーの中身が ics ではありません（URL が切れているかもしれません）")
    return text


def due(url: str, since: date | None = None, days: int = WINDOW_DAYS) -> list[Event]:
    """締切の近い課題を、近い順に。"""
    return due_events(fetch(url), since=since, days=days)


def events(url: str) -> list[Event]:
    """ICS に載る全予定を読む。科目台帳の照合だけに使い、書き込みはしない。"""
    return parse(fetch(url))
