"""本体から来た出来事を受け取って、喋る（A2A の受け口）。

渡ってくるのは「何が起きたか」だけ。言い方と顔は `events.py` が決め、
**喋るのは Realtime のセッション**（`session.py` → `live.py`）に頼む。

本体は返事を待たない（投げっぱなし。docs/voice.md の4節）ので、ここは**すぐ返す**。
喋り終わるまで返さないと、Slack の処理が机の上のロボットの再生時間に引きずられる。

**マイクを閉じているあいだは喋らない**（繋がりが無いので喋る口が無い）。顔だけ変える。
"""

from __future__ import annotations

import json
import logging

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

    def _hold(self, event: dict) -> None:
        """速い道で使えるように、押されてきたものを手元に置く（docs/voice.md の3節）。"""
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
        elif kind == "listen" and self.session is not None:
            # 常に録らない。Slack から入れたときだけ開ける（docs/voice.md の7節）
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
