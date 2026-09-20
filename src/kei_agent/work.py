"""仕事のチャンネル（#30_work）の依頼を、仕事エージェントに取り次ぐ。

いまできるのは Outlook の予定を読むことだけ。返ってくるのは封筒の `data.items` なので、
見せ方（今日・明日・今週）はここで決める。

会社のデータなので、Slack に出すのは**件名・時間・場所・リンクまで**にする。本文は持ち出さない
（docs/plan.md の15章）。

Assistant に混ぜて使う。self.agents、self.post などは Assistant のもの。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from kei_agent import agents, router
from kei_agent.request import Request
from kei_agent.slack_text import FAILED_PREFIX

log = logging.getLogger(__name__)

# 仕事エージェントの仕事の名前（src/kei_agent_work/card.py と同じもの）
LIST_EVENTS = "list-events"
# config.toml の [a2a.agents] で書いたエージェントの名前
AGENT = "work"

CAN_DO = ("このチャンネルでできるのは、いまのところ Outlook の予定を見ることだけだよ。\n"
          "• 「今日の予定は？」「今週の会議教えて」\n"
          "メールや Sharepoint は、まだつないでいない。")
NO_EVENTS = "予定は入っていないよ。"
WEEKDAYS = "月火水木金土日"
# 何日先まで見るか（言われなかったとき）
DEFAULT_DAYS = 7


def _at(value: str) -> datetime | None:
    """Graph が返す時刻（`2026-09-22T10:00:00.0000000`）を読む。"""
    text = (value or "").strip()
    if "." in text:
        head, _, frac = text.partition(".")
        text = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _day(at: datetime) -> str:
    return f"{at.month}/{at.day}（{WEEKDAYS[at.weekday()]}）"


def _line(event: dict, with_day: bool = True) -> str:
    start, end = _at(event.get("start", "")), _at(event.get("end", ""))
    head = f"{_day(start)} " if with_day else ""
    span = "終日" if event.get("all_day") else f"{start:%H:%M}" + (f"–{end:%H:%M}" if end else "")
    where = f"（{event['location']}）" if event.get("location") else ""
    link = f" <{event['url']}|Outlook>" if event.get("url") else ""
    return f"• {head}{span} {event.get('subject', '')}{where}{link}"


def events_of(data: dict) -> list[dict]:
    """封筒の中身から、予定を始まる順に取り出す。"""
    items = [item for item in (data.get("items") or []) if _at(item.get("start", "")) is not None]
    items.sort(key=lambda item: _at(item["start"]))
    return items


def events_text(events: list[dict], now: datetime) -> str:
    """今日・明日・今週に分けて出す。"""
    if not events:
        return NO_EVENTS
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    groups: dict[str, list[dict]] = {"今日": [], "明日": [], "このあと": []}
    for event in events:
        day = _at(event["start"]).date()
        name = "今日" if day == today else "明日" if day == tomorrow else "このあと"
        groups[name].append(event)
    lines = []
    for name, found in groups.items():
        if not found:
            continue
        lines.append(f"*{name}*")
        lines += [_line(event, with_day=name == "このあと") for event in found]
    return "\n".join(lines)


class WorkChannel:
    async def work(self, req: Request) -> None:
        """仕事の依頼を仕事エージェントに渡して、返事をスレッドに出す。"""
        skills = await self.skills_of(AGENT)
        params: dict = {}
        if skills:
            choice = await router.pick(self.config, skills, req.text)
            if choice.skill and choice.skill != router.ASK:
                params = choice.params
            elif choice.skill == router.ASK:
                # 自由な質問に答える口は、道具が増えてから作る
                await self.post(req, CAN_DO)
                await self.mark_answered(req, failed=False)
                return
        reply = await self.ask_work(LIST_EVENTS, **params)
        if not reply.ok:
            await self.post(req, f"{FAILED_PREFIX} {reply.text or '仕事エージェントが止まったよ'}")
            await self.mark_answered(req, failed=True)
            return
        await self.post(req, events_text(events_of(reply.data), datetime.now()))
        await self.mark_answered(req, failed=False)

    async def ask_work(self, skill: str, **params) -> agents.Reply:
        """仕事エージェントに頼む。つながらなければ、その理由を入れた返事にして知らせる。"""
        agent = self.agents.get(AGENT)
        if agent is None:
            await self.notify_trouble("仕事エージェントの住所が config.toml の [a2a.agents] にありません")
            return agents.Reply.broken("仕事エージェントの住所がないよ")
        reply = await agents.ask(agent, skill, params=params or None)
        if not reply.ok:
            await self.notify_trouble(f"仕事エージェント（{agent.base_url}）の {skill} が返した理由: {reply.text[:300]}")
        await self.note_limit(reply)
        return reply
