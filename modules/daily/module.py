"""Daily・振り返りのモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

本体の定期処理 daily（朝）と review（夜）を受け持つ（module.toml の core_schedules）。

- Daily: 研究全体のチャンネルに、今日の予定の一覧（core.morning）を見出しにして出し、そのスレッドに Daily を書く。
  材料は前回の Daily から（core.digest）。共通ホームの日別記録に1日1行で残す
- 振り返り（Retro & Planning）: 今日の成果と未完了を書き、スレッドに明日・明後日の締切を並べる。日別記録のレトプラに
  残す。そのあとスレッドで今日学んだこと・助言を聞き、スレッドを引き取って（core.claim_thread）会話する。AI が聞き返して
  言語化し、まとまったら確かめずに共通ホームの「学びのノート」に1件1ページで残し、日別記録のレトプラにも題とリンクを足す

AI は研究全体の作業場を読むだけで動かす（研究テーマのフォルダとスレッドの記録を読める）。答えが決まった形でなければ
Slack には出さず、日別記録にも残さない。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from kei_agent.api import (
    DIGEST_CHARS,
    AIError,
    Core,
    NotionError,
    Request,
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


# 振り返りのスレッドの記録（日付・日別記録のページ・残した学び）と、会話を受け付ける日数
RETRO = "retro"
RETRO_KEEP_DAYS = 7


def learnings_of(text: str) -> tuple[str, list[dict]]:
    """会話の答えを、Slack に出す本文と、整理した学び（合図の行のあとの JSON）に分ける。学びが無ければ空のリスト。"""
    body = final_answer(text).strip()
    head, marker, rest = body.partition(texts.LEARNING_MARKER)
    if not marker:
        return body, []
    rest = rest.strip()
    try:
        items = json.loads(rest) if rest else []
    except ValueError:
        # JSON のあとに説明が続いたときは、最初の行だけを読む
        try:
            items = json.loads(rest.splitlines()[0])
        except ValueError:
            items = []
    items = [item for item in items if isinstance(item, dict) and str(item.get("title") or "").strip()] \
        if isinstance(items, list) else []
    return head.strip(), items[:3]


class Module:
    def __init__(self, core: Core):
        self.core = core

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """振り返りのスレッドの返事（学びの会話）。聞き返すか、まとまったら学びのノートに残す。"""
        retro = self.core.records.get(RETRO, req.thread_ts)
        if retro is None:
            await self.core.reply(req, "このスレッドの振り返りは、もう受け付けていないよ。")
            return
        history = await self.core.thread_history(req.channel, req.thread_ts)
        try:
            text = await self.core.run_ai("review_talk", texts.talk_prompt(
                retro["day"], history, [item["title"] for item in retro.get("saved") or []], req.text),
                overview=True, trigger="review")
        except AIError as e:
            log.warning("振り返りの会話を続けられませんでした: %s", e)
            await self.core.reply(req, failure_text("review"), failed=True)
            return
        body, items = learnings_of(text)
        if not items:
            await self.core.reply(req, body or failure_text("review"), failed=not body)
            return
        saved = await self._keep(req, retro, items)
        lines = [f"• <{item['url']}|{escape(item['title'])}>" if item.get("url") else f"• {escape(item['title'])}"
                 for item in saved]
        done = "📒 学びのノートに残したよ。直したいときは、ここに書いてね。\n" + "\n".join(lines) if saved else ""
        await self.core.reply(req, "\n\n".join(part for part in (body, done) if part) or failure_text("review"),
                              failed=not saved)

    async def _keep(self, req: Request, retro: dict, items: list[dict]) -> list[dict]:
        """学びを学びのノートに入れ、日別記録のレトプラに題とリンクを足す。前に残したもの（直したとき）は捨てる。"""
        hub = self.core.hub
        if hub is None or not hub.has_learning_db:
            await self.core.notify_trouble("学びのノートに残せませんでした。共通 Notion ホームの共有と "
                                           "kei-agent-hub-setup --apply を確認してください")
            return []
        link = await self.core.permalink(req.channel, req.thread_ts)
        saved = []
        try:
            for old in retro.get("saved") or []:
                await self.core.to_thread(hub.trash_page, old["id"])
            for item in items:
                page_id, url = await self.core.to_thread(hub.add_learning, item, retro["day"], link)
                saved.append({"id": page_id, "url": url, "title": str(item["title"]).strip()})
            if retro.get("note"):
                summary = "\n".join(f"- [{item['title']}]({item['url']})" if item["url"] else f"- {item['title']}"
                                    for item in saved)
                await self.core.to_thread(hub.append_review_conclusion, retro["note"], f"学びのノート\n{summary}",
                                          datetime.now(), req.message_ts)
        except NotionError as e:
            await self.core.notify_trouble(f"学びのノートに残せませんでした: {e}")
        self.core.records.update(RETRO, req.thread_ts, saved=saved)
        return saved

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
        # 今日学んだこと・助言を聞き、返事はこのモジュールが受ける（学びの会話。on_message）
        await self.core.post(channel, texts.RETRO_QUESTION, thread_ts=thread_ts)
        self.core.claim_thread(channel, thread_ts, await self.core.channel_name(channel))
        self.core.records.put(RETRO, thread_ts, {"day": day, "note": note.id if note else "", "saved": []},
                              keep_days=RETRO_KEEP_DAYS)
        return {"status": "posted" if answer else "error", "thread_ts": thread_ts,
                "notion_url": note.url if note else None, "prepared": prepared}
