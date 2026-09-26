"""仕事のチャンネル（#30_work）の依頼を、仕事エージェントに取り次ぐ。

定型は Outlook の予定を読むこと。返ってくるのは封筒の `data.items` なので、
見せ方（今日・明日・今週）はここで決める。

`list-events` から作る一覧は、件名・時間・場所・リンクまで（1行ずつ並べるため）。自由な質問の返事は
仕事エージェントが選択済み provider で組み立てる（長さの加減は prompts/work.md）。会話の続け方は研究と同じ
（Assistant.converse_with_agent）。

Assistant に混ぜて使う。self.agents、self.post などは Assistant のもの。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from kei_agent import agents, router, settings
from kei_agent.request import Request
from kei_agent.response_output import safe_failure
from kei_agent.slack_text import escape

log = logging.getLogger(__name__)

# 仕事エージェントの仕事の名前（src/kei_agent_work/card.py と同じもの）
LIST_EVENTS = "list-events"
ASK = "ask"
# config.toml の [a2a.agents] で書いたエージェントの名前
AGENT = "work"

CAN_DO = ("このチャンネルでできること。\n"
          "• 「今日の予定は？」… Outlook の予定を、今日・明日・このあとで出す\n"
          "• そのほかの質問… メール・Teams・SharePoint を読んで、要点とリンクで答える\n"
          "送信や予定の作成はできない（読むだけ）。")
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
    where = f"（{escape(event['location'])}）" if event.get("location") else ""
    # リンクの中に `|` が入ると、そこから先が表示名になってしまう
    url = str(event.get("url") or "").split("|")[0]
    link = f" <{url}|Outlook>" if url else ""
    return f"• {head}{span} {escape(event.get('subject', ''))}{where}{link}"


def events_of(data: dict) -> list[dict]:
    """封筒の中身から、予定を始まる順に取り出す。"""
    items = [item for item in (data.get("items") or []) if _at(item.get("start", "")) is not None]
    items.sort(key=lambda item: _at(item["start"]))
    return items


def requested_period(question: str) -> str:
    """明示された単一の日付だけを選ぶ。複数日・未指定なら一覧にする。"""
    if "明日" in question and "今日" not in question and "今週" not in question:
        return "tomorrow"
    if "今日" in question and "明日" not in question and "今週" not in question:
        return "today"
    return "week"


def events_text(events: list[dict], now: datetime, period: str = "week") -> str:
    """指定された期間の予定だけを今日・明日・このあとに分けて出す。"""
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    groups: dict[str, list[dict]] = {"今日": [], "明日": [], "このあと": []}
    for event in events:
        day = _at(event["start"]).date()
        if (period == "today" and day != today) or (period == "tomorrow" and day != tomorrow):
            continue
        name = "今日" if day == today else "明日" if day == tomorrow else "このあと"
        groups[name].append(event)
    lines = []
    for name, found in groups.items():
        if not found:
            continue
        lines.append(f"*{name}*")
        lines += [_line(event, with_day=name == "このあと") for event in found]
    return "\n".join(lines) or NO_EVENTS


class WorkChannel:
    async def work(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """仕事の依頼を仕事エージェントに渡して、返事をスレッドに出す。

        すでに振り分けが済んでいるとき（研究全体のチャンネルから回ってきたとき）は、その仕事を使う。
        """
        params = dict(params or {})
        if not skill:
            skills = await self.skills_of(AGENT)
            choice = await router.pick(self.config, skills, req.text, store=self.store) if skills else router.Choice()
            skill, params = choice.skill or ASK, choice.params
        if skill == ASK:
            await self.work_ask(req)
            return
        period = requested_period(req.text)
        if period == "tomorrow" and isinstance(params.get("days"), int):
            params["days"] = max(params["days"], 2)
        reply = await self.ask_work(skill, **params)
        if not reply.ok:
            await self.post(req, safe_failure("connection"))
            await self.mark_answered(req, failed=True)
            return
        await self.post(req, events_text(events_of(reply.data), datetime.now(), period))
        await self.mark_answered(req, failed=False)

    async def work_ask(self, req: Request) -> None:
        """自由な質問を仕事エージェントに渡す。会話の続け方・経過・上限・出力の確認は研究と同じ。"""
        await self.converse_with_agent(req, AGENT)

    async def ask_work(self, skill: str, **params) -> agents.Reply:
        """仕事エージェントに頼む。つながらなければ、その理由を入れた返事にして知らせる。"""
        agent = self.agents.get(AGENT)
        if agent is None:
            await self.notify_trouble("仕事エージェントの住所が config.toml の [a2a.agents] にありません")
            return agents.Reply.broken("仕事エージェントの住所がないよ")
        provider = settings.selected_provider(self.config, self.store, AGENT)
        reply = await agents.ask(agent, skill, params={**params, "provider": provider})
        if not reply.ok:
            await self.notify_trouble(f"仕事エージェント（{agent.base_url}）の {skill} が返した理由: {reply.text[:300]}")
        await self.note_limit(reply, AGENT, provider)
        return reply
