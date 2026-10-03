"""大学の担当プロセス（A2A のサーバー）。起動は共通のコマンド `kei-agent-module course`。

Moodle の課題、Notion の授業と課題（ゲートウェイの course として授業ホームだけに届く）、Toggl の実績の
機械的な同期・一覧取得を扱う。相談と資料の読み書きは Dot が行う。

metadata の `skill`（なければ本文の1行目）で、どの仕事かを決める。細かい指定（`days`・`weekday`）は、本体（module.py）
が本文の JSON で渡す。返事は全担当で共通の封筒。締切の一覧は `data.items` に入れ、見せ方は本体が決める
。時限の時刻と学期は、学校の設定（school.py）で決まる。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime

from kei_agent_a2a.api import (
    WEEKDAYS,
    AgentSkill,
    NotionError,
    SkillExecutor,
    TaskUpdater,
    TogglError,
    requested_days,
    weekday,
)

from . import moodle, moodle_api, notion_sync, submissions, toggl_report
from .ics import Event
from .school import SchoolError, from_config, next_weekday
from .skills import (
    LIST_CALENDAR_ASSIGNMENTS,
    LIST_CLASSES,
    LIST_CURRENT_COURSES,
    LIST_DUE,
    SYNC_ASSIGNMENTS,
    SYNC_SUBMISSIONS,
    TIME_REPORT,
)

log = logging.getLogger(__name__)

DESCRIPTION = "Moodle の課題と提出・受験状態を同期し、授業・課題と Toggl の実績を機械的に返す"
SKILLS = [
    AgentSkill(
        id=SYNC_ASSIGNMENTS,
        name="課題を取り込む",
        description="Moodle から履修中の科目と課題を読み、Notion の授業・課題データベースに反映する。"
                    "新しく増えた課題と、締切が変わった課題を返す",
        tags=["moodle", "notion"],
        examples=["課題を取り込んで", "Moodle を見てきて"],
    ),
    AgentSkill(
        id=SYNC_SUBMISSIONS,
        name="提出・受験状態を同期する",
        description="認証付き Moodle API で課題の提出・小テストの受験終了を確認し、"
                    "Notion の対応する課題を提出済みにする。未提出・取得失敗では状態を戻さない",
        tags=["moodle", "notion"],
        examples=["提出状態を確認して", "小テストの受験終了を反映して"],
    ),
    AgentSkill(
        id=LIST_DUE,
        name="締切の近い課題",
        description="締切が近い順に JSON で返す（items: id/at/course/title/url）。"
                    "既定では2週間先まで。本文の JSON の days で変えられる",
        tags=["moodle"],
        examples=["今週の締切は？", "明日までの課題を教えて"],
    ),
    AgentSkill(
        id=LIST_CALENDAR_ASSIGNMENTS,
        name="課題カレンダー用の全件取得",
        description="授業ホームの課題 DB から指定期間の締切を省略せず読み取る。"
                    "data.complete/items: id/title/due/status/url/course/moodle/moodle_id。書き込みはしない",
        tags=["notion", "calendar", "read-only"],
        examples=[],
    ),
    AgentSkill(
        id=LIST_CLASSES,
        name="その日の授業",
        description="履修中の科目のうち、その曜日のものを時刻つきで JSON で返す"
                    "（data.items: subject/weekday/period/start/end）。"
                    "既定は今日。本文の JSON の weekday（月〜日）で変えられる",
        tags=["notion"],
        examples=["今日の授業は？", "金曜の時間割"],
    ),
    AgentSkill(
        id=LIST_CURRENT_COURSES,
        name="今学期の履修科目",
        description="今学期に履修中の科目だけを JSON で返す（items: id/subject/weekday/period）",
        tags=["notion", "course"],
        examples=["履修中の授業", "今学期の科目"],
    ),
    AgentSkill(
        id=TIME_REPORT,
        name="実績時間の集計",
        description="Toggl の記録を科目ごと・課題ごとに集計して返す（読むだけ）。"
                    "既定は直近7日。本文の JSON の days で変えられる",
        tags=["toggl"],
        examples=["今週、どの授業に何時間使った？", "先週の実績を見せて"],
    ),
]
NAMES = tuple(skill.id for skill in SKILLS)
NO_ICS = ("Moodle のカレンダーの URL がありません。Moodle のカレンダー画面で「カレンダーをエクスポートする」から "
          f"URL を作って、秘密情報のファイルの {moodle.ICS_ENV} に入れてください")
# 一度に返す締切の数（声やスレッドで読める長さに収める）
MAX_DUE = 20
# 「締切の近い課題」で見る先の長さ（日）。取り込みは学期の終わりまで見るので、こちらだけ短くする
DUE_DAYS = 14
NO_PERIODS = ("（時限の時刻が分からないので、時刻は空にしたよ。config.toml の [course] に school か periods を"
              "書いてね）")


def asked_skill(text: str, metadata: dict | None = None) -> str:
    """どの仕事を頼まれたか。metadata の skill を優先し、なければ本文の1行目から探す。"""
    asked = (metadata or {}).get("skill")
    if not isinstance(asked, str):
        asked = (text or "").strip().splitlines()[0].strip() if text.strip() else ""
    return asked if asked in NAMES else ""


def asked_weekday(text: str) -> str:
    """本文の JSON の weekday（月〜日）。無ければ空文字（今日）。"""
    try:
        value = (json.loads(text) or {}).get("weekday")
    except (TypeError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) and len(value) == 1 and value in WEEKDAYS else ""


def due_data(items: list[dict], days: int, *, now: datetime | None = None) -> dict:
    """Notion の状態を使い、提出済みと期限切れを締切一覧から除く。"""
    now = now or datetime.now().astimezone()
    pending = []
    for item in items:
        if item.get("status") == "提出済み":
            continue
        at = datetime.fromisoformat(item["due"])
        if len(item["due"]) == 10:
            # 日付だけの手入力はその日全体の締切。朝から期限切れにしない。
            at = at.replace(hour=23, minute=59, second=59, tzinfo=now.tzinfo)
        at = at.astimezone(now.tzinfo)
        if at < now:
            continue
        pending.append((at, item))
    pending.sort(key=lambda pair: (pair[0], pair[1]["id"]))
    return {
        "days": days,
        "more": max(0, len(pending) - MAX_DUE),
        "items": [{"id": item.get("moodle_id") or item["id"], "at": at.isoformat(),
                   "course": item.get("course", ""), "title": item["title"], "url": item.get("url", "")}
                  for at, item in pending[:MAX_DUE]],
    }


class Executor(SkillExecutor):
    def __init__(self, config=None, store=None):
        super().__init__(config, store)
        # 定期チェックと手動同期が同時に来ても、同じ課題を二度更新しない。
        self._sync_lock = asyncio.Lock()

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = asked_skill(text, metadata)
        if not skill:
            await self.fail(updater, f"どの仕事か分かりませんでした。{' / '.join(NAMES)} のどれかを "
                                      "metadata の skill か、本文の1行目に書いてください")
            return
        log.info("頼まれた仕事: %s", skill)
        handlers = {SYNC_ASSIGNMENTS: self._sync_assignments, SYNC_SUBMISSIONS: self._sync_submissions,
                    LIST_DUE: self._list_due,
                    LIST_CALENDAR_ASSIGNMENTS: self._list_calendar_assignments,
                    LIST_CLASSES: self._list_classes, LIST_CURRENT_COURSES: self._list_current_courses,
                    TIME_REPORT: self._time_report}
        await handlers[skill](updater, metadata, text)

    async def _due_events(self, updater: TaskUpdater, days: int) -> list[Event] | None:
        """Moodle のカレンダーから締切を読む。読めなければ理由を返して None。"""
        url = moodle.ics_url()
        if not url:
            await self.fail(updater, NO_ICS)
            return None
        try:
            return await asyncio.to_thread(lambda: moodle.due(url, days=days))
        except moodle.MoodleError as e:
            await self.fail(updater, str(e))
            return None

    async def _sync_assignments(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        async with self._sync_lock:
            await self._import_assignments(updater, metadata, text)

    async def _submission_result(self) -> submissions.Result:
        cursor = (self.records.get("submissions", "cursor") or {}).get("after", "")
        result = await asyncio.to_thread(submissions.sync, after=cursor)
        submissions.record_result(result, self.records)
        return result

    async def _sync_submissions(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        async with self._sync_lock:
            try:
                result = await self._submission_result()
            except (moodle_api.APIError, notion_sync.SyncError, NotionError) as error:
                await self.fail(updater, str(error))
                return
            await self.done(updater, result.summary(), asdict(result))

    async def _import_assignments(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """Moodle の締切を Notion の「課題」に反映する。"""
        events = await self._due_events(updater, requested_days(text, moodle.WINDOW_DAYS))
        if events is None:
            return
        try:
            result = await asyncio.to_thread(notion_sync.sync, events)
        except (notion_sync.SyncError, NotionError) as e:
            await self.fail(updater, str(e))
            return
        try:
            status = await self._submission_result()
        except (moodle_api.APIError, notion_sync.SyncError, NotionError) as error:
            status = submissions.Result(errors=[str(error)])
        summary = result.summary() + ("\n" + status.summary() if status.enabled else "")
        await self.done(updater, summary, {
            "added": result.added, "updated": result.updated, "unchanged": result.unchanged,
            "other_courses": result.other_courses, "completed": status.completed, "errors": status.errors,
            "submissions_enabled": status.enabled})

    async def _list_due(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """締切の近い課題を、近い順に JSON で返す。"""
        days = requested_days(text, DUE_DAYS)
        try:
            snapshot = await asyncio.to_thread(notion_sync.list_calendar_assignments, days, date.today())
            if snapshot.get("complete") is not True:
                raise notion_sync.SyncError("課題 DB を全部読めませんでした")
            data = due_data(snapshot["items"], days)
        except (notion_sync.SyncError, NotionError, ValueError, KeyError, TypeError) as error:
            await self.fail(updater, str(error))
            return
        await self.done(updater, f"これから {days} 日で締切の課題は {len(data['items'])} 件", data)

    async def _list_calendar_assignments(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """Notion 課題 DB の完全な read-only snapshot を返す。"""
        days = requested_days(text, 30)
        try:
            data = await asyncio.to_thread(notion_sync.list_calendar_assignments, days, date.today())
        except (notion_sync.SyncError, NotionError) as e:
            await self.fail(updater, str(e))
            return
        await self.done(updater, f"{days} 日間の課題締切は {len(data['items'])} 件", data)

    async def _list_classes(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """その曜日の授業を、時刻つきで返す（朝のまとめで時系列に並べるために使う）。

        曜日を指定されたら、今日から見て次のその曜日の日付で、学期と時刻を決める。
        """
        today = date.today()
        wanted = asked_weekday(text) or weekday(today)
        day = next_weekday(wanted, today)
        try:
            school = from_config(self.config)
            found = await asyncio.to_thread(notion_sync.courses_on, wanted, day, school=school)
        except (notion_sync.SyncError, NotionError, SchoolError) as e:
            await self.fail(updater, str(e))
            return
        items = []
        for course in found:
            span = school.at(day, course["period"])
            items.append({**course,
                          "start": span[0].isoformat(timespec="minutes") if span else "",
                          "end": span[1].isoformat(timespec="minutes") if span else ""})
        note = NO_PERIODS if not school.periods and any(item["period"] for item in items) else ""
        await self.done(updater, f"{wanted}曜の授業は {len(items)} コマ{note}",
                         {"weekday": wanted, "items": items})

    async def _list_current_courses(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        try:
            items = await asyncio.to_thread(notion_sync.current_courses, school=from_config(self.config))
        except (notion_sync.SyncError, NotionError, SchoolError) as e:
            await self.fail(updater, str(e))
            return
        await self.done(updater, f"今学期の履修科目は {len(items)} 件", {"items": items})

    async def _time_report(self, updater: TaskUpdater, metadata: dict, text: str = "") -> None:
        """Toggl の記録を、科目ごと・課題ごとに集計して返す。"""
        note = ""
        try:
            courses = await asyncio.to_thread(notion_sync.course_names)
        except (notion_sync.SyncError, NotionError) as e:
            # 科目が読めないだけで集計をやめるより、全部数えて、そう言ったほうが役に立つ
            courses, note = None, "\n（「授業」を読めなかったので、Toggl のプロジェクト全部を数えたよ）"
            log.warning("「授業」を読めませんでした: %s", e)
        try:
            text = await asyncio.to_thread(
                toggl_report.report, requested_days(text, toggl_report.DEFAULT_DAYS), courses)
        except TogglError as e:
            await self.fail(updater, str(e))
            return
        await self.done(updater, text + note)
