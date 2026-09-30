"""大学のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 大学のチャンネル（#20_course）… 言われたことを担当の名刺の仕事から選んで頼み、返ってきた中身を Slack 向けに
  組み直して出す。定型（取り込む・締切・実績）に当てはまらない質問は、担当が Box と授業ホームを読んで答える
- 予定（agenda）… 今日と明日の授業（🎓）と、締切（⏰）を、朝の一覧・声・振り返りの材料に出す
- 取り込み（prepare）… Daily と振り返りの前に、Moodle の課題を授業ホームに取り込み、増えた・変わった課題を知らせる
- 見回り（tick）… 1時間に1回、締切まで24時間を切った課題と、3日を切っても「未着手」の課題を1件ずつ1回だけ知らせる。
  毎朝8時を過ぎたら、授業ホームの課題（これからの全部）を共通ホームの予定カレンダーに写す（出典「課題」）

本体では AI を動かさない（担当が動かす）。締切は封筒の `data.items` で返ってくるので、見せ方はここで決める。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from kei_agent.api import (
    ASK,
    Core,
    Request,
    checked_text,
    day_label,
    due_clock,
    due_day,
    escape,
    failure_text,
    parse_time,
    weekday,
)

from .skills import LIST_CALENDAR_ASSIGNMENTS, LIST_CLASSES, LIST_DUE, SYNC_ASSIGNMENTS, TIME_REPORT

log = logging.getLogger(__name__)

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
# 近い締切が無いとき、いちばん近いものを探す先の長さ（日）。大学エージェントが受け付ける上限
NEAREST_DAYS = 400
# 「一番近い」「次の締切」のように、1件を聞かれたときの言い方（振り分け係が件数を拾えなかったときの当て）
ONE = re.compile(r"(一番|いちばん|最も|もっとも)[^。？?]{0,8}近い|直近の|次の(締切|締め切り|課題)")
# まとめて知らせる締切までの時間と、それを見に行く間隔（秒）
SOON_HOURS = 24
DUE_CHECK_SECONDS = 3600
# 締切まで何日を切っても「未着手」なら知らせるか（24時間前の知らせより先に気づけるように）
EARLY_DAYS = 3
UNSTARTED = "未着手"
SUBMITTED = "提出済み"


# 予定カレンダーに写すのは毎朝この時刻から。写せなかったら、この間隔でやり直す（秒）。課題はこれからの全部
CALENDAR_HOUR = 8
CALENDAR_RETRY_SECONDS = 3600
CALENDAR_DAYS = 400
CALENDAR_SOURCE = "課題"
# 予定（agenda）に出す授業の日数（今日と明日。朝の一覧は今日、振り返りの材料は明日の授業を使う）
CLASS_DAYS = 2


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
    items = [item for item in (data.get("items") or []) if parse_time(item.get("at")) is not None]
    items.sort(key=lambda item: parse_time(item.get("at")))
    return items, int(data.get("more") or 0)


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


def _line(item: dict, with_day: bool = True, now: datetime | None = None) -> str:
    at = parse_time(item.get("at"))
    head = f"{day_label(due_day(at))} " if with_day else ""
    course = f"{escape(item['course'])} / " if item.get("course") else ""
    left = f"（{_left(at, now)}）" if now is not None else ""
    return f"• {head}{due_clock(at)} {course}{escape(item.get('title', ''))}{left}"


def due_text(items: list[dict], more: int = 0, now: datetime | None = None) -> str:
    """スレッドへの返事。近い順に並べるだけ。"""
    if not items:
        return NO_DUE
    lines = [_line(item, now=now) for item in items]
    if more:
        lines.append(f"（ほかに {more} 件）")
    return "\n".join(lines)


def first_items(items: list[dict], limit: int) -> list[dict]:
    """近い順の先頭 limit 件。同じ日時の締切は分けない（「一番近い」に、同時のものを落とさない）。"""
    if limit >= len(items):
        return items
    last = parse_time(items[limit - 1].get("at"))
    return [item for n, item in enumerate(items) if n < limit or parse_time(item.get("at")) == last]


def nearest_text(items: list[dict], days: int, now: datetime) -> str:
    """見た期間には締切が無いときの返事。その先のいちばん近いもの（同じ日時のものも）を添える。"""
    span = "2週間" if days == 14 else f"{days}日"
    if not items:
        return NO_DUE
    return "\n".join([f"これから{span}の締切はないよ。いちばん近いのはこれ。",
                      *(_line(item, now=now) for item in first_items(items, 1))])


def due_soon_items(items: list[dict], now: datetime, hours: int = SOON_HOURS, days: int = EARLY_DAYS) -> list[dict]:
    """まとめて知らせる課題（Notion の課題）。近い順に並べる。過ぎたものは入れない。

    あと hours 時間以内で提出済みでないもの、または、あと days 日以内で「未着手」のもの。
    """
    soon_limit = now + timedelta(hours=hours)
    early_limit = now + timedelta(days=days)
    found = []
    for item in items:
        at = parse_time(item.get("due"))
        if at is None or at < now:
            continue
        status = item.get("status")
        if (status != SUBMITTED and at <= soon_limit) or (status == UNSTARTED and at <= early_limit):
            found.append(item)
    found.sort(key=lambda item: parse_time(item.get("due")))
    return found


def _due_soon_label(at: datetime, now: datetime) -> str:
    """締切までの言い方。今日締切なら `~ HH:MM`、それ以外は `あと N 日`。"""
    day = due_day(at)
    if day <= now.date():
        return f"~ {due_clock(at)}"
    return f"あと{(day - now.date()).days}日"


def due_soon_line(item: dict, now: datetime) -> str:
    """まとめて知らせる締切の1行（科目／課題名（Moodle のリンク））。"""
    at = parse_time(item["due"])
    course = f"{escape(item['course'])}／" if item.get("course") else ""
    url = f"（{item['moodle']}）" if item.get("moodle") else ""
    return f"• {_due_soon_label(at, now)}：{course}{escape(item.get('title', ''))}{url}"


def due_soon_text(items: list[dict], now: datetime) -> str:
    """締切が近い課題を1通にまとめて知らせる文。締切が増えたら、この一覧を全部出し直す。"""
    return "\n".join(["締切の課題", *(due_soon_line(item, now) for item in items)])


def due_soon_key(item: dict) -> str:
    """同じ課題を二度知らせないための目印（締切が動いたら、また知らせる）。

    Moodle 由来の課題は、朝の一覧の締切（notice_key）と同じ形にする。朝すでに出したものを、ここでまた知らせない。
    """
    if item.get("moodle_id"):
        return f"due:{item['moodle_id']}:{item.get('due', '')}"
    return f"early:{item.get('id', '')}:{item.get('due', '')}"


def notice_key(item: dict) -> str:
    """同じ締切を二度知らせないための目印（締切が動いたら、また知らせる）。"""
    return f"due:{item.get('id', '')}:{item.get('at', '')}"


class Module:
    default_question = "授業について教えて"

    def __init__(self, core: Core):
        self.core = core
        # 締切の知らせを最後に見た時刻と、予定カレンダーに課題を写そうとした時刻（起動直後に1回見る）
        self._due_checked = 0.0
        self._calendar_tried = 0.0

    def welcome(self) -> str:
        return CAN_DO

    # 大学のチャンネル

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """大学の依頼。研究全体のチャンネルから回ってきたときは、振り分け係が選んだ仕事を使う。"""
        params = dict(params or {})
        if not skill:
            if await self.core.skills():
                skill, params = await self.core.pick_skill(req)
            else:
                # 名刺が読めないときは、言葉で当てる
                skill = pick_skill(req.text) or ASK
        if skill == ASK:
            # 定型に当てはまらない質問は、担当が Box と授業ホームを読んで答える
            await self.core.converse(req)
            return
        reply = await self.core.ask_agent(skill, params)
        if not reply.ok:
            await self.core.reply(req, failure_text(), failed=True)
            return
        if skill == LIST_DUE:
            await self.core.reply(req, await self.due_answer(req, reply.data, params))
            return
        text = checked_text(reply.text)
        if text is None:
            await self.core.reply(req, failure_text("conversation"), failed=True)
            return
        await self.core.reply(req, text)

    async def due_answer(self, req: Request, data: dict, params: dict) -> str:
        """締切の一覧への返事。1件を聞かれたら1件で、見た期間に無ければ、その先のいちばん近いものを添える。"""
        now = datetime.now()
        items, more = due_items(data)
        limit = params.get("limit") or (1 if ONE.search(req.text or "") else None)
        if items and limit:
            shown = first_items(items, limit)
            return due_text(shown, 0 if limit == 1 else more + len(items) - len(shown), now)
        days = int(data.get("days") or 0)
        if items or not days or days >= NEAREST_DAYS:
            return due_text(items, more, now)
        later = await self.core.ask_agent(LIST_DUE, {"days": NEAREST_DAYS})
        return nearest_text(due_items(later.data)[0] if later.ok else [], days, now)

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
            # 朝の一覧に出した締切は、この目印で24時間前の知らせを繰り返さない
            found += [{**item, "kind": "due", "notice": notice_key(item)} for item in items]
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
        """Moodle の課題を授業ホームに取り込む。増えた課題と締切の変わった課題は、大学のチャンネルに知らせる。"""
        reply = await self.core.ask_agent(SYNC_ASSIGNMENTS, {})
        if not reply.ok:
            return False
        added = [escape(str(title)) for title in reply.data.get("added") or []]
        updated = [escape(str(title)) for title in reply.data.get("updated") or []]
        channel = await self.course_channel() if (added or updated) else None
        if channel:
            count = "・".join(f"{label} {len(items)}件" for label, items in (("新着", added), ("締切変更", updated))
                             if items)
            lines = [f"• {t}" for t in added] + [f"• :repeat: {t}" for t in updated]
            await self.core.post(channel, "\n".join([f"📚 Moodle の課題（{count}）", *lines]))
        return True

    # 見回り

    async def tick(self, now: datetime) -> None:
        await self.notify_due_soon(now)
        await self.sync_calendar(now)

    async def notify_due_soon(self, now: datetime) -> None:
        """締切が近い課題（24時間以内・3日以内で未着手）を、1通にまとめて知らせる。

        締切が増えたら、その時点の一覧を全部出し直す（1時間に1回見る）。
        """
        if now.timestamp() - self._due_checked < DUE_CHECK_SECONDS:
            return
        channel = await self.course_channel()
        if channel is None:
            return
        reply = await self.core.ask_agent(LIST_CALENDAR_ASSIGNMENTS, {"days": EARLY_DAYS + 1})
        if not reply.ok:
            # 取れなかったときは時計を進めない（1時間待たずに、次の見回りで取り直す）
            return
        self._due_checked = now.timestamp()
        items = due_soon_items(reply.data.get("items") or [], now)
        if not items or all(self.core.noticed(due_soon_key(item)) for item in items):
            return
        await self.core.post(channel, due_soon_text(items, now))
        for item in items:
            self.core.emit("due", title=item.get("title"), at=item.get("due"))
            self.core.mark_noticed(due_soon_key(item))

    async def sync_calendar(self, now: datetime) -> None:
        """毎朝8時を過ぎたら、授業ホームの課題（これからの全部）を共通ホームの予定カレンダーに写す。

        全部を読めたと言い切れるとき（complete）だけ写す。写せなかったら1時間おきにやり直す。
        """
        day = now.date().isoformat()
        if now.hour < CALENDAR_HOUR or self.core.hub is None:
            return
        if (self.core.records.get("calendar", "synced") or {}).get("day") == day:
            return
        if now.timestamp() - self._calendar_tried < CALENDAR_RETRY_SECONDS:
            return
        self._calendar_tried = now.timestamp()
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
