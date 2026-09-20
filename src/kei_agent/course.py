"""大学のチャンネル（#20_course）の依頼を、大学エージェントに取り次ぐ。

オーケストレーター（Kei Agent 本体）は、言われたことがどの仕事にあたるかだけを決めて A2A で頼み、
返ってきた中身を Slack 向けの形にして出す。本体では claude を動かさない。
定型（取り込む・締切・実績）に当てはまらない質問は `ask` に回し、**大学エージェント自身の claude** が
Box と Notion を読んで答える（docs/agents.md）。

締切は封筒の `data.items` で返ってくるので、見せ方はここで決める（スレッドへの返事、朝の一覧、
24時間前の知らせ）。

Assistant に混ぜて使う。self.agents、self.post などは Assistant のもの。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from kei_agent import agents, router
from kei_agent.request import Request
from kei_agent.slack_text import FAILED_PREFIX

log = logging.getLogger(__name__)

# 大学エージェントの仕事の名前（src/kei_agent_course/card.py と同じもの）。
# あちらは a2a-sdk に依存していて本体からは読み込めないので、文字列で持つ
SYNC_ASSIGNMENTS = "sync-assignments"
LIST_DUE = "list-due"
TIME_REPORT = "time-report"
# 定型に当てはまらない質問の窓口（どのエージェントでも同じ名前。docs/agents.md）
ASK = "ask"
# config.toml の [a2a.agents] で書いたエージェントの名前
AGENT = "course"

# 振り分け係（軽いモデル）が答えられなかったときの、言葉での当て。上から順に見る
WORDS = (
    (SYNC_ASSIGNMENTS, ("取り込", "取りこ", "同期", "反映", "moodle", "ムードル", "見てきて", "最新")),
    (TIME_REPORT, ("何時間", "実績", "toggl", "トグル", "集計", "どのくらいやった", "どれくらいやった")),
    (LIST_DUE, ("締切", "しめきり", "締め切り", "期限", "課題", "いつまで", "提出", "残ってる", "todo")),
)
CAN_DO = ("このチャンネルでできること。\n"
          "• 「課題を取り込んで」… Moodle の締切を Notion の「課題」に入れる\n"
          "• 「締切を教えて」… 近い順に、これから2週間ぶんを出す\n"
          "• 「今週どれくらいやった？」… Toggl の記録を科目ごとに集計する\n"
          "• そのほかの質問… Box の学部要項と過去問、Notion の授業と課題を読んで答える")
NO_DUE = "締切の近い課題はないよ。"
WEEKDAYS = "月火水木金土日"
# 朝の一覧で見る先の長さ（日）と、個別に知らせる締切までの時間
DIGEST_DAYS = 7
SOON_HOURS = 24


def pick_skill(text: str) -> str:
    """言われたことから、どの仕事かを決める。分からなければ空文字。"""
    lowered = (text or "").lower()
    for skill, words in WORDS:
        if any(word in lowered for word in words):
            return skill
    return ""


# 締切の中身（大学エージェントの list-due が返す JSON）


def due_items(data: dict) -> tuple[list[dict], int]:
    """封筒の中身から、締切の一覧と「ほかに何件あるか」を取り出す。"""
    items = [item for item in (data.get("items") or []) if _at(item) is not None]
    items.sort(key=lambda item: _at(item))
    return items, int(data.get("more") or 0)


def _at(item: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(item.get("at"))).replace(tzinfo=None)
    except ValueError:
        return None


def _left(at: datetime, now: datetime) -> str:
    """締切まで、あとどれくらいか。"""
    minutes = (at - now).total_seconds() / 60
    if minutes < 0:
        return "締切を過ぎてる"
    if minutes < 60:
        return f"あと {int(minutes)} 分"
    if minutes < 60 * 24:
        return f"あと {int(minutes / 60)} 時間"
    return f"あと {int(minutes / 60 / 24)} 日"


def _day(at: datetime) -> str:
    return f"{at.month}/{at.day}（{WEEKDAYS[at.weekday()]}）"


def _line(item: dict, with_day: bool = True, now: datetime | None = None) -> str:
    at = _at(item)
    head = f"{_day(at)} " if with_day else ""
    course = f"{item.get('course')} / " if item.get("course") else ""
    left = f"（{_left(at, now)}）" if now is not None else ""
    return f"• {head}{at:%H:%M} {course}{item.get('title', '')}{left}"


def due_text(items: list[dict], more: int = 0, now: datetime | None = None) -> str:
    """スレッドへの返事。近い順に並べるだけ。"""
    if not items:
        return NO_DUE
    lines = [_line(item, now=now) for item in items]
    if more:
        lines.append(f"（ほかに {more} 件）")
    return "\n".join(lines)


def digest_text(items: list[dict], now: datetime) -> str:
    """朝の一覧。今日・明日・今週に分ける。"""
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    groups: dict[str, list[dict]] = {"今日": [], "明日": [], "今週": []}
    for item in items:
        at = _at(item)
        if at.date() == today:
            groups["今日"].append(item)
        elif at.date() == tomorrow:
            groups["明日"].append(item)
        elif at.date() <= (now + timedelta(days=DIGEST_DAYS)).date():
            groups["今週"].append(item)
    lines = [f"📅 授業の締切（{_day(now)}）"]
    for name, found in groups.items():
        if not found:
            continue
        lines.append(f"*{name}*")
        lines += [_line(item, with_day=name == "今週", now=now if name == "今日" else None) for item in found]
    if len(lines) == 1:
        lines.append(f"これから {DIGEST_DAYS} 日のうちに締切の課題はないよ。")
    return "\n".join(lines)


def soon_items(items: list[dict], now: datetime, hours: int = SOON_HOURS) -> list[dict]:
    """あと hours 時間以内に締切のもの（過ぎたものは入れない）。"""
    limit = now + timedelta(hours=hours)
    return [item for item in items if now <= _at(item) <= limit]


def soon_text(item: dict, now: datetime) -> str:
    """締切が近いものを1件ずつ知らせる文。"""
    at = _at(item)
    course = f"{item.get('course')} / " if item.get("course") else ""
    url = f"\n{item['url']}" if item.get("url") else ""
    return (f"⏰ {_left(at, now)}で締切: {course}{item.get('title', '')}\n"
            f"{_day(at)} {at:%H:%M} まで{url}")


def notice_key(item: dict) -> str:
    """同じ締切を二度知らせないための目印（締切が動いたら、また知らせる）。"""
    return f"due:{item.get('id', '')}:{item.get('at', '')}"


class CourseChannel:
    async def course(self, req: Request) -> None:
        """大学の依頼を大学エージェントに渡して、返事をスレッドに出す。"""
        skill, params = await self.course_skill(req)
        if skill == ASK:
            # 定型に当てはまらない質問は、大学エージェントの claude が Box と Notion を読んで答える
            await self.course_ask(req)
            return
        reply = await self.ask_course(skill, **params)
        if not reply.ok:
            await self.post(req, f"{FAILED_PREFIX} {reply.text or '大学エージェントが止まったよ'}")
            await self.mark_answered(req, failed=True)
            return
        if skill == LIST_DUE:
            items, more = due_items(reply.data)
            await self.post(req, due_text(items, more, datetime.now()))
        else:
            await self.post(req, reply.text or "（返事が空だったよ）")
        await self.mark_answered(req, failed=False)

    async def course_skill(self, req: Request) -> tuple[str, dict]:
        """どの仕事かを決める。軽いモデルに選ばせ、選べなければ言葉で当てて、最後は `ask`。"""
        skills = await self.skills_of(AGENT)
        if skills:
            ui = self.thread_ui(req)
            await ui.activity(router.STATUS_TEXT)
            choice = await router.pick(self.config, skills, req.text)
            if choice.skill:
                return choice.skill, choice.params
        return pick_skill(req.text) or ASK, {}

    async def course_ask(self, req: Request) -> None:
        """自由な質問を大学エージェントに渡す。経過は1行に出し、返事は流して見せる。"""
        ui = self.thread_ui(req)
        await ui.start()
        session_id = self.store.agent_session(req.channel, req.thread_ts, AGENT)
        payload = json.dumps({
            "prompt": req.text or "授業について教えて",
            "session_id": session_id,
            "channel": req.channel,
            "thread_ts": req.thread_ts,
        }, ensure_ascii=False)

        async def on_progress(raw: str) -> None:
            try:
                event = json.loads(raw)
            except ValueError:
                return
            if event.get("activity"):
                await ui.activity(event["activity"])
            elif event.get("text"):
                await ui.text(event["text"])

        reply = await self.ask_course(ASK, text=payload, on_progress=on_progress)
        answer = reply.text.strip()
        if session_id and not reply.ok and "No conversation found" in str(reply.data.get("errors")):
            # エージェントを入れ替えると会話が消える。1回だけ、続きなしで聞き直す
            self.store.set_agent_session(req.channel, req.thread_ts, AGENT, "")
            reply = await self.ask_course(ASK, text=payload.replace(f'"{session_id}"', "null"),
                                          on_progress=on_progress)
            answer = reply.text.strip()
        new_session = str(reply.data.get("session_id") or "")
        if new_session:
            self.store.set_agent_session(req.channel, req.thread_ts, AGENT, new_session)
        if not reply.ok:
            answer = f"{FAILED_PREFIX} {answer or '大学エージェントが止まったよ'}"
        streamed = await ui.finish(answer or "（返事が空だったよ）")
        if not streamed:
            await self.post(req, answer or "（返事が空だったよ）", markdown=True)
        await self.mark_answered(req, failed=not reply.ok)

    async def ask_course(self, skill: str, text: str = "", on_progress=None, **params) -> agents.Reply:
        """大学エージェントに頼む。つながらなければ、その理由を入れた返事にして知らせる。"""
        agent = self.agents.get(AGENT)
        if agent is None:
            await self.notify_trouble("大学エージェントの住所が config.toml の [a2a.agents] にありません")
            return agents.Reply.broken("大学エージェントの住所がないよ")
        reply = await agents.ask(agent, skill, params=params or None, text=text, on_progress=on_progress)
        if not reply.ok:
            await self.notify_trouble(f"大学エージェント（{agent.base_url}）の {skill} が返した理由: {reply.text[:300]}")
        await self.note_limit(reply)
        return reply

    async def course_due(self, days: int, now: datetime) -> list[dict] | None:
        """締切の一覧をもらう。取れなければ None（知らせは ask_course が出す）。"""
        reply = await self.ask_course(LIST_DUE, days=days)
        if not reply.ok:
            return None
        items, _ = due_items(reply.data)
        return items
