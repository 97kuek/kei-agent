"""Daily・振り返りのモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

本体の定期処理 daily（朝）と review（夜）を受け持つ（module.toml の core_schedules）。

- Daily: 研究全体のチャンネルに、今日の予定の一覧（core.morning）を見出しにして出し、そのスレッドに Daily を書く。
  材料は前回の Daily から（core.digest）。共通ホームの日別記録に1日1行で残す
- 振り返り（Retro & Planning）: 今日の成果と未完了を書き、スレッドに明日・明後日の締切を並べる。日別記録のレトプラに
  残し、あとからスレッドに貼られた結論も同じ行に足す（core.collect_conclusions）

AI は研究全体の作業場を読むだけで動かす（研究テーマのフォルダとスレッドの記録を読める）。答えが決まった形でなければ
Slack には出さず、日別記録にも残さない。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from kei_agent.api import (
    DIGEST_CHARS,
    AIError,
    Core,
    NotionError,
    checked_sections,
    day_label,
    due_clock,
    due_day,
    escape,
    failure_text,
    final_answer,
    parse_time,
)

from . import texts

log = logging.getLogger(__name__)


def soon_deadlines(dues: list[dict], now: datetime, days: int) -> str:
    """いまから days 日後の終わりまでの締切（振り返りで、明日の計画に使う）。無ければ空文字。"""
    limit = now.date() + timedelta(days=days)
    found = sorted(((at, item) for item in dues
                    if (at := parse_time(item.get("at", ""))) and now <= at and due_day(at) <= limit),
                   key=lambda pair: pair[0])
    if not found:
        return ""
    lines = ["📌 明日・明後日の締切"]
    for at, item in found:
        course = f"{escape(item['course'])} " if item.get("course") else ""
        lines.append(f"`{day_label(due_day(at))} {due_clock(at)}` {course}{escape(item.get('title', ''))}")
    return "\n".join(lines)


def daily_answer(text: str) -> str:
    """Daily の答えのうち、Slack に出す4つの見出し。形が違えば空文字。"""
    return checked_sections(final_answer(text), texts.DAILY_HEADINGS) or ""


def review_answer(text: str) -> str:
    """振り返りの答えのうち、Slack に出す2つの見出しと最後の1行（無ければ足す）。形が違えば空文字。"""
    body = final_answer(text).strip()
    if body.endswith(texts.NIGHT_QUESTION):
        body = body[:-len(texts.NIGHT_QUESTION)].rstrip()
    shown = checked_sections(body, texts.REVIEW_HEADINGS)
    return f"{shown}\n\n{texts.NIGHT_QUESTION}" if shown else ""


class Module:
    def __init__(self, core: Core):
        self.core = core

    async def run_schedule(self, name: str, day: str) -> dict:
        if name == "daily":
            return await self.daily(day)
        if name == "review":
            return await self.review(day)
        return {"status": "error", "error": f"知らない定期処理です: {name}"}

    async def _overview(self) -> str:
        """研究全体のチャンネルの ID（Kei Agent が入っていなければ空文字）。"""
        return (await self.core.channel_ids()).get(self.core.channels("overview")[0], "")

    async def _write(self, use_case: str, kind: str, prompt: str) -> str:
        """AI に書いてもらい、Slack に出す形にした答え。動かせなかった・形が違ったら空文字。"""
        try:
            text = await self.core.run_ai(use_case, prompt, overview=True, trigger=kind)
        except AIError as e:
            log.warning("%s を書けませんでした: %s", kind, e)
            return ""
        shown = daily_answer(text) if kind == "daily" else review_answer(text)
        if not shown:
            log.warning("%s の答えが決まった形ではありません", kind)
        return shown

    async def _save(self, channel: str, thread_ts: str, title: str, kind: str, day: str, markdown: str):
        """共通ホームの日別記録に1日1行で残す。残せなくても Slack には出ているので、知らせるだけにする。"""
        hub = self.core.hub
        if not markdown.strip():
            return None
        if hub is None:
            await self.core.notify_trouble(
                f"{title} を日別記録に保存できませんでした。共通 Notion ホームが使えません"
                "（Slack には出ています。共有と kei-agent-hub-setup を確認してください）")
            return None
        try:
            link = await self.core.permalink(channel, thread_ts)
            return await self.core.to_thread(hub.upsert_day, kind, day, title, markdown, link)
        except NotionError as e:
            await self.core.notify_trouble(f"{title} を日別記録に保存できませんでした: {e}")
            return None

    async def daily(self, day: str) -> dict:
        channel = await self._overview()
        if not channel:
            return {"status": "no_channel"}
        since = self.core.last_ran("daily", before_day=day) or time.time() - 86400
        digest = await self.core.digest(since, time.time(), f"Daily の材料 {day}")
        answer = await self._write("daily_write", "daily",
                                   texts.daily_prompt(day, texts.material(digest, DIGEST_CHARS)))
        title = f"Daily {day_label(day)}"
        # チャンネルには今日の予定だけ、スレッドに Daily の見出しと中身
        morning = await self.core.morning(datetime.now())
        thread_ts = await self.core.publish(channel, morning.text, f"**🌅 {title}**\n\n{answer or failure_text('daily')}")
        self.core.mark_shown(morning.notices)
        note = await self._save(channel, thread_ts, title, "Daily", day, answer) if answer else None
        return {"status": "posted" if answer else "error", "thread_ts": thread_ts,
                "notion_url": note.url if note else None, "morning": morning.detail}

    async def review(self, day: str) -> dict:
        channel = await self._overview()
        if not channel:
            return {"status": "no_channel"}
        now = datetime.now()
        # 明日の計画に使うので、振り返りの前にモジュールが取り込み直す（大学なら Moodle の課題）
        prepared = await self.core.gather_prepare("review", day)
        since = datetime.combine(date.fromisoformat(day), dtime(0, 0)).timestamp()
        digest = await self.core.digest(since, time.time(), f"Retro & Planning の材料 {day}", agenda=True)
        answer = await self._write("review_write", "review",
                                   texts.review_prompt(day, texts.material(digest, DIGEST_CHARS)))
        title = f"Retro & Planning {day_label(day)}"
        thread_ts = await self.core.publish(channel, f"🌙 {title}", answer or failure_text("review"))
        # モジュールの締切を並べる（締切だけを頼む。会議を AI でもう一度読まない）
        agenda, _ = await self.core.gather_agenda(texts.REVIEW_DUE_DAYS + 1, {"due"})
        deadlines = soon_deadlines([item for items in agenda.values() for item in items], now, texts.REVIEW_DUE_DAYS)
        if deadlines:
            await self.core.post(channel, deadlines, thread_ts=thread_ts)
        note = None
        if answer:
            note = await self._save(channel, thread_ts, title, "振り返り", day,
                                    "\n\n".join(part for part in (answer, deadlines, texts.REVIEW_QUESTIONS) if part))
            if note:
                # このスレッドに貼られた結論を、同じ行のレトプラに足す
                self.core.collect_conclusions(channel, thread_ts, note.id)
        return {"status": "posted" if answer else "error", "thread_ts": thread_ts,
                "notion_url": note.url if note else None, "prepared": prepared}
