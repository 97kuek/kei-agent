"""Toggl の記録を、科目ごと・課題ごとに集計する（読むだけ）。

プロジェクト＝科目、エントリの説明＝課題名で突き合わせる（docs/design.md の11章）。
測るのは依頼者自身。ここでは足し合わせて返すだけで、計測の開始も停止もしない。
Toggl の鍵は研究の時間記録と同じものを使う（`kei_agent.timelog`）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from kei_agent.timelog import Toggl, TogglError, load_toggl

log = logging.getLogger(__name__)

DEFAULT_DAYS = 7
NO_TOGGL = ("Toggl の鍵がありません（TOGGL_API_TOKEN と TOGGL_ORGANIZATION_ID、"
            "TOGGL_WORKSPACE_ID を秘密情報のファイルに入れてください）")
NO_NAME = "（説明なし）"
# 科目ごとに出す課題の数（多いと読みにくい）
MAX_TASKS = 4


def _hours(seconds: float) -> float:
    return round(seconds / 3600, 1)


def totals(entries: list[dict], courses: Iterable[str] | None = None) -> dict[str, dict[str, float]]:
    """{科目: {課題: 秒}}。`courses` を渡すと、その科目だけ数える。

    動かしっぱなしの記録（duration が空か負）、休憩、消した記録は数えない。
    """
    known = set(courses) if courses is not None else None
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for entry in entries:
        duration = entry.get("duration")
        if not isinstance(duration, int | float) or duration <= 0:
            continue
        if entry.get("type") == "break" or entry.get("deleted_at"):
            continue
        course = (entry.get("project") or {}).get("name") or "-"
        if course.startswith("大学 / "):
            course = course.removeprefix("大学 / ").strip() or "-"
        if known is not None and course not in known:
            continue
        out[course][(entry.get("description") or "").strip() or NO_NAME] += float(duration)
    return {course: dict(tasks) for course, tasks in out.items()}


def lines(by_course: dict[str, dict[str, float]], since: date, until: date) -> list[str]:
    """Slack にそのまま出せる短い行にする。"""
    span = f"{since.month}/{since.day}〜{until.month}/{until.day}"
    if not by_course:
        return [f"{span} は、科目の記録がなかったよ。"
                "Toggl のプロジェクト名を「授業」の科目名と同じにすると数えられる。"]
    out = [f"{span} の実績（Toggl）"]
    total = 0.0
    for course, tasks in sorted(by_course.items(), key=lambda kv: -sum(kv[1].values())):
        seconds = sum(tasks.values())
        total += seconds
        out.append(f"• {course}: {_hours(seconds)} 時間")
        for name, task_seconds in sorted(tasks.items(), key=lambda kv: -kv[1])[:MAX_TASKS]:
            out.append(f"    - {name}: {_hours(task_seconds)} 時間")
        if len(tasks) > MAX_TASKS:
            out.append(f"    - （ほか {len(tasks) - MAX_TASKS} 件）")
    out.append(f"合計: {_hours(total)} 時間")
    return out


def report(days: int = DEFAULT_DAYS, courses: Iterable[str] | None = None,
           toggl: Toggl | None = None, today: date | None = None) -> str:
    """直近 days 日（今日を含む）の実績。"""
    toggl = toggl or load_toggl()
    if toggl is None:
        raise TogglError(NO_TOGGL)
    until = today or date.today()
    since = until - timedelta(days=max(days, 1) - 1)
    return "\n".join(lines(totals(toggl.entries(since, until), courses), since, until))
