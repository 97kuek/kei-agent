"""朝の一覧（今日の予定を時刻順に1通）を組み立てる。本体の窓口 core.morning と、本体の定期処理から使う。

集めるもの: モジュールの取り込み（prepare）、予定（agenda）、朝の一覧の行（morning_notes）、前回の Daily から
うまくいかなかった定期処理。会議は出典ごとに共通ホームの予定カレンダーにも写し、声のレイヤには1週間ぶんの予定を
出来事（schedule）で渡す。文の組み立ては morning.py。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from kei_agent import morning, settings
from kei_agent.calendar_sync import JST, CalendarSnapshot, IncompleteSnapshot, outlook_items, sync_calendar
from kei_agent.notion import NotionError

if TYPE_CHECKING:
    from kei_agent.assistant import Assistant

log = logging.getLogger(__name__)

# 声のレイヤに渡す日数。「明日の予定」「今週の予定」に答えられるように1週間ぶん
VOICE_DAYS = 7


@dataclass(frozen=True)
class Morning:
    """朝の一覧。text は Slack にそのまま出せる形。"""
    text: str
    # 集めた件数と、予定カレンダーに写した結果（定期処理の記録に残す）
    detail: dict
    # Slack に出せたら記録する目印（朝に出した締切を、24時間前の知らせで繰り返さないため）
    notices: tuple[str, ...]


async def sync_meetings(assistant: Assistant, events: list[dict], now: datetime, source: str) -> dict | str:
    """朝に読んだ会議（7日ぶん）を、共通ホームの予定カレンダーに足す（出典は source）。

    AI が読んだ一覧は全部とは言い切れないので、見つからなくなった会議は消さずに「要確認」にする
    （0件のときは読み損ねを疑って、印も付けない）。
    """
    hub = assistant.hub
    if hub is None:
        return "no_hub"
    try:
        snapshot = CalendarSnapshot(source, False, outlook_items(events))
        report = await asyncio.to_thread(sync_calendar, hub, snapshot, now.astimezone(JST), VOICE_DAYS)
    except (IncompleteSnapshot, NotionError, ValueError, TypeError) as e:
        log.warning("会議を予定カレンダーに書けません: %s", e)
        return "error"
    except Exception:
        # 朝のまとめは止めない
        log.exception("会議を予定カレンダーに書けません")
        return "error"
    return report.__dict__


def failure_note(assistant: Assistant, now: datetime, failed_now: list[str] | None = None) -> str:
    """前回の Daily から今朝までに、うまくいかなかった定期処理を1行で。無ければ空文字。

    Daily とレトプラが何日も Notion に残っていなかったのに、気づけなかった（2026-09-26）。
    """
    failed = settings.failed_schedules(assistant.config, assistant.store, now) + (failed_now or [])
    return f"⚠️ うまくいかなかったこと: {'、'.join(dict.fromkeys(failed))}" if failed else ""


def notes(assistant: Assistant, now: datetime, failed_now: list[str] | None = None, skip: str = "") -> list[str]:
    """時刻の無いもの（モジュールの今朝の分、うまくいかなかったこと）を、1行ずつ。"""
    found = assistant.module_notes(now.date().isoformat(), skip)
    failure = failure_note(assistant, now, failed_now)
    return [*found, failure] if failure else found


async def build(assistant: Assistant, now: datetime, skip: str = "") -> Morning:
    """朝の一覧（今日の時系列）。集められなかったものは黙って飛ばす。skip は行を足さないモジュール（呼んだモジュール）。"""
    detail: dict = {}
    classes: list[dict] = []
    dues: list[dict] = []
    events: list[dict] = []
    # 朝に出した締切は、そのあと24時間前の知らせで繰り返さない。ただし記録するのは
    # Slack に出せたあと（出す前に記録すると、投稿に失敗したときに黙って消える）
    notices: list[str] = []
    # モジュールは、まず取り込み直す（大学なら Moodle の課題）
    prepared = await assistant.module_prepare("daily", now.date().isoformat())
    # モジュールの予定（agenda。仕事なら Outlook の会議、大学なら授業と締切）。声のレイヤが「今週の会議」に
    # 答えられるように1週間ぶん取り、朝の一覧と声に載せ、会議は出典ごとに予定カレンダーにも書く
    agenda, unread = await assistant.module_agenda(VOICE_DAYS)
    synced: dict[str, dict | str] = {}
    for name, items in agenda.items():
        meetings = [item for item in items if item.get("kind", "meeting") == "meeting"]
        events += meetings
        classes += [item for item in items if item.get("kind") == "class"]
        module_dues = [item for item in items if item.get("kind") == "due"]
        dues += module_dues
        # モジュールの締切の目印は、そのモジュールの core.notice_once と同じ名前で記録する
        notices += [f"module.{name}.{item['notice']}" for item in morning.soon(module_dues, now) if item.get("notice")]
        for source in dict.fromkeys(str(item.get("source") or name) for item in meetings):
            synced[source] = await sync_meetings(
                assistant, [item for item in meetings if str(item.get("source") or name) == source], now, source)
    if synced or unread:
        detail["agenda"] = {"synced": synced, "unread": unread}
    detail |= {"classes": len(classes), "dues": len(dues), "events": len(events)}
    # 声のレイヤは、聞かれてから取りに行かず、朝に決まったものを手元へ渡しておく（出来事 schedule）。
    # 渡すのはデータで、声の言い方は声のレイヤが作る（帯も URL も声では読めない）。
    # **日付も渡す。** 今日ぶんだけ渡していたせいで、明日を聞かれても今日を答えていた
    assistant.emit("schedule", items=[
        {"date": f"{e.day:%Y-%m-%d}", "at": e.clock,
         "end": f"{e.end:%H:%M}" if e.end else "", "icon": e.icon, "text": e.text}
        for e in morning.upcoming(classes, events, dues, now, days=VOICE_DAYS)])
    failed_now = ["会議の書き込み"] if "error" in synced.values() else []
    failed_now += prepared + [f"{label}の予定の読み取り" for label in unread]
    text = morning.text(classes, events, dues, now, notes(assistant, now, failed_now, skip))
    return Morning(text, detail, tuple(notices))
