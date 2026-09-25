"""本体から来た出来事を受け取って、喋る（A2A の受け口）。

渡ってくるのは「何が起きたか」だけ。言い方と顔は `events.py` が決め、
**喋るのは Realtime のセッション**（`session.py` → `live.py`）に頼む。

本体は返事を待たない（投げっぱなし。docs/architecture.md の「声のレイヤ」）ので、ここは**すぐ返す**。
喋り終わるまで返さないと、Slack の処理が机の上のロボットの再生時間に引きずられる。

**マイクを閉じているあいだは喋らない**（繋がりが無いので喋る口が無い）。顔だけ変える。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent_a2a import envelope
from kei_agent_voice import events
from kei_agent_voice.card import NOTIFY

log = logging.getLogger(__name__)

SKILLS = (NOTIFY,)


def message_text(context: RequestContext) -> str:
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


def event_of(text: str, metadata: dict) -> dict:
    """出来事を取り出す。本文が JSON ならそれを、そうでなければ metadata を使う。"""
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            found = json.loads(stripped)
        except ValueError:
            found = None
        if isinstance(found, dict):
            return found
    return {k: v for k, v in metadata.items() if k != "skill"}


def current(held: dict, now: datetime | None = None) -> dict:
    """古くなったものを捨てて返す。終わった数は**その日のぶん**、上限はやり直しの時刻まで。"""
    now = now or datetime.now()
    today = now.date().isoformat()
    until = _reset_at(str(held.get("limited_until") or ""))
    if held.get("day") != today:
        if held.get("day") and until is None:
            # やり直しの時刻が読めなかった上限は、日が変わったら忘れる
            held.pop("limited", None)
        held.pop("done", None)
        held.pop("failed", None)
        held["day"] = today
    if held.get("limited") and until is not None and _passed(until, now):
        held.pop("limited", None)
        held.pop("limited_until", None)
    return held


def _reset_at(at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(at)
    except ValueError:
        return None


def _passed(reset: datetime, now: datetime) -> bool:
    if reset.tzinfo is not None:
        now = now.astimezone() if now.tzinfo is None else now
    elif now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return now >= reset


class VoiceExecutor(AgentExecutor):
    def __init__(self):
        # 朝のまとめなど、喋らずに手元へ置くもの（道具が読む）
        self.held: dict[str, dict] = {}
        # 喋る・マイクを開け閉めする相手（app.py が立ち上げのときに入れる）
        self.session = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()

        skill = metadata.get("skill", NOTIFY)
        if skill not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} だけです")
            return

        event = event_of(message_text(context), metadata)
        found = events.reaction(event)
        if found is None:
            await self._fail(updater, f"知らない出来事です: {event.get('kind')!r}")
            return
        self._hold(event)

        log.info("知らせを受け取りました: %s（喋る: %s）", event.get("kind"), found.speaks)
        # 喋るのは投げっぱなし。本体を待たせない
        self._react(found)
        await updater.complete(updater.new_agent_message(
            [Part(text=envelope.reply("受け取ったよ", {"spoke": found.speaks, "face": found.face}))]))

    def _hold(self, event: dict, now: datetime | None = None) -> None:
        """速い道で使えるように、押されてきたものを手元に置く（docs/architecture.md の「声のレイヤ」）。"""
        current(self.held, now)
        kind = str(event.get("kind") or "")
        if kind == "schedule":
            self.held["schedule"] = event
        elif kind == "working":
            self.held["running"] = int(self.held.get("running") or 0) + 1
        elif kind in ("done", "failed"):
            self.held["running"] = max(int(self.held.get("running") or 0) - 1, 0)
            self.held[kind] = int(self.held.get(kind) or 0) + 1
        elif kind == "limited":
            self.held["limited"] = True
            self.held["limited_until"] = str(event.get("reset_at") or "")
        elif kind == "listen" and self.session is not None:
            # 常に録らない。Slack から入れたときだけ開ける（docs/architecture.md の「声のレイヤ」）
            self.session.set_listening(bool(event.get("on")))

    def _react(self, found: events.Reaction) -> None:
        if self.session is None:
            return
        try:
            self.session.announce(found.text if found.speaks else "", found.face)
        except Exception:
            # 喋れなくても本体の仕事は終わっている。落ちた理由だけ残す
            log.exception("知らせを声に出せませんでした")

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([Part(text=envelope.failure(reason))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
