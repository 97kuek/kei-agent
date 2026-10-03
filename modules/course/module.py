"""大学のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 大学のチャンネル（#2-course）… 同期結果を知らせる。質問には Dot での相談を案内する
- 予定（agenda）… 今日と明日の授業（🎓）と、締切（⏰）を、朝の一覧・声・振り返りの材料に出す
- 取り込み（prepare）… Daily と振り返りの前に、Moodle の課題を授業ホームに取り込み、増えた・変わった課題を知らせる
- 見回り（tick）… 起動・復帰時と30分ごとに締切を取り込み、提出・受験終了は10分ごとに確認する。
  課題を予定カレンダーへ同期する。締切通知は Dot が行う。
  毎朝8時を過ぎたら、授業ホームの課題（これからの全部）を共通ホームの予定カレンダーに写す（出典「課題」）

AI による相談は Dot が担当し、このモジュールは機械的な同期と予定の取得を行う。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from time import monotonic

from kei_agent.api import Core, Request, escape, parse_time, weekday

from .skills import LIST_CALENDAR_ASSIGNMENTS, LIST_CLASSES, LIST_DUE, SYNC_ASSIGNMENTS, SYNC_SUBMISSIONS

log = logging.getLogger(__name__)

# 予定カレンダーに写すのは毎朝この時刻から。写せなかったら、この間隔でやり直す（秒）。課題はこれからの全部
CALENDAR_HOUR = 8
CALENDAR_RETRY_SECONDS = 3600
CALENDAR_DAYS = 400
CALENDAR_SOURCE = "課題"
SUBMISSIONS_INTERVAL_SECONDS = 600
ASSIGNMENTS_INTERVAL_SECONDS = 1800
RESUME_GAP_SECONDS = 300
# 予定（agenda）に出す授業の日数（今日と明日。朝の一覧は今日、振り返りの材料は明日の授業を使う）
CLASS_DAYS = 2


def due_items(data: dict) -> tuple[list[dict], int]:
    """封筒の中身から、締切の一覧と「ほかに何件あるか」を取り出す。"""
    items = [item for item in (data.get("items") or []) if parse_time(item.get("at")) is not None]
    items.sort(key=lambda item: parse_time(item.get("at")))
    return items, int(data.get("more") or 0)


class Module:
    def __init__(self, core: Core):
        self.core = core
        # 予定カレンダーに課題を写そうとした時刻（起動直後に1回見る）
        self._calendar_tried = 0.0
        self._calendar_dirty_tried = None
        self._submissions_tried = 0.0
        self._assignments_tried = 0.0
        self._assignments_pending = True
        self._tick_finished = None
        self._assignments_task = None
        self._assignments_submissions_enabled = False

    async def head_action(self, name: str, params: dict) -> dict | None:
        if name != "sync_submissions":
            return None
        reply = await self.core.ask_agent(SYNC_SUBMISSIONS, {})
        if reply.ok:
            await self.submission_problems(reply.data)
            if reply.data.get("completed"):
                await self.sync_calendar(datetime.now())
        return {**reply.data, "ok": reply.ok and not bool(reply.data.get("errors")), "text": reply.text}

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """通知用チャンネルに届いた質問には、Dot の相談先だけを案内する。"""
        await self.core.reply(req, "大学の相談は Dot に頼んでね。Notion の授業・課題と Box の資料を見て答えるよ。")

    async def course_channel(self) -> str | None:
        """大学のチャンネルの ID。Kei Agent がいなければ None。"""
        names = self.core.channels("course")
        return (await self.core.channel_ids()).get(names[0]) if names else None

    async def dues(self, days: int) -> list[dict] | None:
        """これから days 日の締切（近い順）。取れなければ None（知らせは ask_agent が出す）。"""
        reply = await self.core.ask_agent(LIST_DUE, {"days": days})
        return due_items(reply.data)[0] if reply.ok else None

    # 予定（朝の一覧・声・振り返りの材料）

    async def agenda(self, days: int, kinds: frozenset[str] | None = None) -> list[dict] | None:
        """今日と明日の授業と、これから days 日の締切。読めなければ None。"""
        found: list[dict] = []
        if kinds is None or "class" in kinds:
            today = date.today()
            for offset in range(min(days, CLASS_DAYS)):
                reply = await self.core.ask_agent(LIST_CLASSES, {"weekday": weekday(today + timedelta(days=offset))})
                if not reply.ok:
                    return None
                found += [{**item, "kind": "class"} for item in reply.data.get("items") or [] if isinstance(item, dict)]
        if kinds is None or "due" in kinds:
            items = await self.dues(days)
            if items is None:
                return None
            found += [{**item, "kind": "due"} for item in items]
        return found

    # Daily と振り返りの前の取り込み

    async def prepare(self, kind: str, day: str) -> list[str]:
        """Moodle の課題を授業ホームに取り込む。朝は、昨日までに予定カレンダーへ写せていたかも見る。"""
        failed = [] if await self.sync_assignments() else ["課題の取り込み"]
        if kind == "daily" and self.core.hub is not None:
            last = (self.core.records.get("calendar", "synced") or {}).get("day")
            yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            if last and last < yesterday:
                failed.append("予定カレンダーへの課題の書き込み")
        return failed

    async def sync_assignments(self) -> bool:
        """朝の取り込みと見回りが重なったら、同じ取り込みと通知の完了を待つ。"""
        if self._assignments_task is None or self._assignments_task.done():
            self._assignments_task = asyncio.create_task(self._sync_assignments())
        return await self._assignments_task

    async def _sync_assignments(self) -> bool:
        """Moodle の課題を授業ホームに取り込み、増えた課題と締切の変わった課題を知らせる。"""
        reply = await self.core.ask_agent(SYNC_ASSIGNMENTS, {})
        self._assignments_submissions_enabled = reply.data.get("submissions_enabled") is True
        if not reply.ok:
            return False
        await self.submission_problems(reply.data)
        added = [escape(str(title)) for title in reply.data.get("added") or []]
        updated = [escape(str(title)) for title in reply.data.get("updated") or []]
        completed = [escape(str(title)) for title in reply.data.get("completed") or []]
        channel = await self.course_channel() if (added or updated or completed) else None
        if channel:
            count = "・".join(f"{label} {len(items)}件" for label, items in
                             (("新着", added), ("締切変更", updated), ("提出済み", completed))
                             if items)
            lines = ([f"• {t}" for t in added] + [f"• :repeat: {t}" for t in updated]
                     + [f"• ✅ {t}" for t in completed])
            await self.core.post(channel, "\n".join([f"📚 Moodle の課題（{count}）", *lines]))
        return not bool(reply.data.get("errors"))

    # 見回り

    async def submission_problems(self, data: dict) -> None:
        if data.get("errors"):
            await self.core.notify_trouble("Moodle の提出状態の同期が一部できませんでした: " + str(data["errors"][0]))

    async def sync_submissions(self) -> bool:
        reply = await self.core.ask_agent(SYNC_SUBMISSIONS, {})
        if not reply.ok:
            return False
        await self.submission_problems(reply.data)
        completed = [escape(str(title)) for title in reply.data.get("completed") or []]
        channel = await self.course_channel() if completed else None
        if channel:
            await self.core.post(channel, "\n".join([f"✅ Moodle の提出・受験終了（{len(completed)}件）",
                                                    *(f"• {title}" for title in completed)]))

        return reply.data.get("enabled") is True and not bool(reply.data.get("errors"))

    async def tick(self, now: datetime) -> None:
        started = monotonic()
        stamp = now.timestamp()
        resumed = self._tick_finished is None or stamp - self._tick_finished >= RESUME_GAP_SECONDS
        if resumed:
            self._assignments_pending = True
        try:
            interval = SUBMISSIONS_INTERVAL_SECONDS if self._assignments_pending else ASSIGNMENTS_INTERVAL_SECONDS
            if resumed or stamp - self._assignments_tried >= interval:
                self._assignments_tried = self._submissions_tried = stamp
                self._assignments_pending = True
                # 締切の取り込みは提出同期も含む。同じ見回りで二度呼ばない。
                if await self.sync_assignments():
                    self._assignments_pending = False
                    self.core.records.put("moodle", "synced", {"at": now.isoformat()})
                    if self._assignments_submissions_enabled:
                        self.core.records.put("moodle", "submissions_synced", {"at": now.isoformat()})
            elif not self._assignments_pending and stamp - self._submissions_tried >= SUBMISSIONS_INTERVAL_SECONDS:
                self._submissions_tried = stamp
                if await self.sync_submissions():
                    self.core.records.put("moodle", "submissions_synced", {"at": now.isoformat()})
            await self.sync_calendar(now)
        finally:
            # 長い取り込み自体をスリープと判定しない。失敗した見回りも完了時刻を残す。
            self._tick_finished = stamp + monotonic() - started

    async def sync_calendar(self, now: datetime) -> None:
        """毎朝8時以降と提出状態の変更時に、課題を共通ホームの予定カレンダーへ写す。

        全部を読めたと言い切れるとき（complete）だけ写す。写せなかったら1時間おきにやり直す。
        """
        day = now.date().isoformat()
        dirty_record = self.core.records.get("calendar", "dirty")
        dirty = bool((dirty_record or {}).get("dirty"))
        changed = dirty and dirty_record != self._calendar_dirty_tried
        if (now.hour < CALENDAR_HOUR and not dirty) or self.core.hub is None:
            return
        if not dirty and (self.core.records.get("calendar", "synced") or {}).get("day") == day:
            return
        if not changed and now.timestamp() - self._calendar_tried < CALENDAR_RETRY_SECONDS:
            return
        self._calendar_tried = now.timestamp()
        self._calendar_dirty_tried = dirty_record
        reply = await self.core.ask_agent(LIST_CALENDAR_ASSIGNMENTS, {"days": CALENDAR_DAYS})
        data = reply.data if reply.ok else {}
        if data.get("complete") is not True or not isinstance(data.get("items"), list):
            log.warning("授業ホームの課題を全部は読めなかったので、予定カレンダーに写しません")
            return
        items = [{"id": item.get("id"), "title": item.get("title"), "start": item.get("due"),
                  "url": item.get("url"), "status": item.get("status")} if isinstance(item, dict) else item
                 for item in data["items"]]
        report = await self.core.sync_calendar(CALENDAR_SOURCE, items, day=day, days=CALENDAR_DAYS, complete=True,
                                               expected_count=data.get("source_count"))
        if isinstance(report, dict):
            self.core.records.put("calendar", "synced", {"day": day, **report})
            # 読み取り中に別の提出が完了したら、次の見回りで再び同期する。
            if self.core.records.get("calendar", "dirty") == dirty_record:
                self.core.records.put("calendar", "dirty", {"dirty": False})
