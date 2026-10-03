"""仕事のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 仕事のチャンネル（#3-work）… 言われたことを担当の名刺の仕事から選び、予定の一覧なら Outlook の予定を
  今日・明日・このあとに分けて出す。そのほかの質問は、担当が Microsoft 365 を読んで答える（会話の続き方は研究と同じ）
- プロジェクトのチャンネル（module.toml の [channels] project に "work-*"。#work-billing など）… 依頼はプロジェクトの
  作業場で担当と会話して答える（core.work。添付・できたファイル・引き継ぎは研究テーマと同じ流れ）
- 予定（agenda）… 朝の一覧・声のレイヤ・共通ホームの予定カレンダー（出典 Outlook）・振り返りの材料に、会議を出す

`list-events` から作る一覧は、件名・時間・場所・リンクまで（1行ずつ並べるため）。自由な質問の返事は、担当が
選択済み provider で組み立てる（長さの加減は work.md）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from kei_agent.api import Core, Request, day_label, escape, failure_text, parse_time, theme_name

from .skills import LIST_EVENTS

log = logging.getLogger(__name__)

CAN_DO = ("このチャンネルでできること。\n"
          "• 「今日の予定は？」… Outlook の予定を、今日・明日・このあとで出す\n"
          "• そのほかの質問… メール・Teams・SharePoint を読んで、要点とリンクで答える\n"
          "送信や予定の作成はできない（読むだけ）。\n"
          "コードを書くときは、プロジェクトごとに `#work-<名前>` のチャンネルを作って招いてね。")
NO_EVENTS = "予定は入っていないよ。"
# 何日先まで見るか（言われなかったとき）
DEFAULT_DAYS = 7
# 共通ホームの予定カレンダーに書くときの出典
SOURCE = "Outlook"


def _line(event: dict, with_day: bool = True) -> str:
    start, end = parse_time(event.get("start", "")), parse_time(event.get("end", ""))
    head = f"{day_label(start)} " if with_day else ""
    span = "終日" if event.get("all_day") else f"{start:%H:%M}" + (f"–{end:%H:%M}" if end else "")
    where = f"（{escape(event['location'])}）" if event.get("location") else ""
    # リンクの中に `|` が入ると、そこから先が表示名になってしまう
    url = str(event.get("url") or "").split("|")[0]
    link = f" <{url}|Outlook>" if url else ""
    join_url = str(event.get("join_url") or "").split("|")[0]
    join = f" <{join_url}|参加>" if join_url else ""
    passcode = f" パスコード: {escape(event['passcode'])}" if event.get("passcode") else ""
    return f"• {head}{span} {escape(event.get('subject', ''))}{where}{link}{join}{passcode}"


def events_of(data: dict) -> list[dict]:
    """封筒の中身から、予定を始まる順に取り出す。"""
    items = [item for item in (data.get("items") or []) if parse_time(item.get("start", "")) is not None]
    items.sort(key=lambda item: parse_time(item["start"]))
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
        day = parse_time(event["start"]).date()
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


def is_project(channel_name: str, patterns: tuple[str, ...]) -> bool:
    """プロジェクトのチャンネル（[channels] project の "work-*" に頭が一致する名前。番号は外して比べる）か。"""
    name = theme_name(channel_name)
    heads = [pattern[:-1] for pattern in patterns if pattern.endswith("-*")]
    return any(name.startswith(head) and len(name) > len(head) for head in heads)


class Module:
    default_question = "今日の予定は？"

    def __init__(self, core: Core):
        self.core = core


    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """仕事の依頼。プロジェクトのチャンネルなら作業場で答える。研究全体のチャンネルから回ってきたときは、
        振り分け係が選んだ仕事を使う。"""
        if is_project(req.channel_name, self.core.channels("project")):
            await self.core.work(req)
            return
        params = dict(params or {})
        if not skill:
            skill, params = await self.core.pick_skill(req)
        if skill != LIST_EVENTS:
            # 自由な質問は、担当が Microsoft 365 を読んで答える
            await self.core.converse(req)
            return
        period = requested_period(req.text)
        days = params.get("days") if isinstance(params.get("days"), int) else DEFAULT_DAYS
        if period == "tomorrow":
            days = max(days, 2)
        reply = await self.core.ask_agent(LIST_EVENTS, {"days": days})
        if not reply.ok:
            await self.core.reply(req, failure_text(), failed=True)
            return
        await self.core.reply(req, events_text(events_of(reply.data), datetime.now(), period))

    async def agenda(self, days: int, kinds: frozenset[str] | None = None) -> list[dict] | None:
        """これから days 日の会議（朝の一覧・声・予定カレンダー・振り返りの材料）。読めなければ None。

        会議を頼まれていないとき（振り返りで締切だけを集めるときなど）は、AI で Outlook を読まない。
        """
        if kinds is not None and "meeting" not in kinds:
            return []
        reply = await self.core.ask_agent(LIST_EVENTS, {"days": days})
        if not reply.ok:
            return None
        return [{**event, "kind": "meeting", "source": SOURCE} for event in events_of(reply.data)]
