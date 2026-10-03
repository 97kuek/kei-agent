"""声の担当プロセス（A2A サーバーと、マイクの会話）。起動は共通のコマンド `kei-agent-module voice`。

本体から来た出来事を受け取って喋る。渡ってくるのは「何が起きたか」だけで、言い方と顔は `events.py` が決め、
**喋るのは Realtime のセッション**（`session.py` → `live.py`）に頼む。

本体は返事を待たない（投げっぱなし。docs/architecture.md の「声」）ので、ここは**すぐ返す**。
喋り終わるまで返さないと、MCP の処理が机の上のロボットの再生時間に引きずられる。

A2A サーバーとマイクを同じプロセスで持つ（background）。**マイクは既定では開けない**。開け閉めは
MCP の「聞く（Mac のマイク）」の通知設定から、本体側（module.py）が出来事 listen として押してくる。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from kei_agent_a2a.api import AgentSkill, SkillExecutor, TaskUpdater

from . import events
from .held import current
from .session import VoiceSession
from .skills import LISTEN_KEY, NOTIFY, SWITCH

log = logging.getLogger(__name__)

DESCRIPTION = ("Mac の音声サービス。本体から出来事を受け取り、言い方と表情を決める。"
               "Stack-chan がいればそちらで、いなければ Mac のスピーカーで鳴らす")
SKILLS = [
    AgentSkill(
        id=NOTIFY,
        name="知らせる",
        description="出来事を受け取って、喋る・顔を変える。渡すのは何が起きたかだけ（kind と、その中身）。"
                    "文と顔と首は声のレイヤが組み立てる。kind: schedule（朝のまとめ。喋らず手元に置く）、"
                    "working（依頼を受けた。顔だけ）、done（終わった）、failed（止まった）、"
                    "limited（契約の上限）、awaiting（返事待ち）、listen（マイクを開ける・閉じる）",
        tags=["voice"],
        examples=['{"kind": "done", "theme": "amr-query"}'],
    ),
]


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
    return {k: v for k, v in metadata.items() if k not in ("skill", "provider")}


class Executor(SkillExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 朝のまとめなど、喋らずに手元へ置くもの（道具が読む）
        self.held: dict[str, dict] = {}
        # 喋る・マイクを開け閉めする相手（background が立ち上げのときに入れる）
        self.session = None

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = metadata.get("skill", NOTIFY)
        if skill != NOTIFY:
            await self.fail(updater, f"できるのは {NOTIFY} だけです")
            return
        event = event_of(text, metadata)
        if event.get("kind") == "listen":
            if not isinstance(event.get("on"), bool):
                await self.fail(updater, "マイクの設定 on は true / false を指定してください")
                return
            if self.session is None:
                await self.fail(updater, "マイクを切り替える準備ができていません。起動後にやり直してください")
                return
        found = events.reaction(event)
        if found is None:
            await self.fail(updater, f"知らない出来事です: {event.get('kind')!r}")
            return
        self._hold(event)
        log.info("知らせを受け取りました: %s（喋る: %s）", event.get("kind"), found.speaks)
        # 喋るのは投げっぱなし。本体を待たせない
        self._react(found)
        await self.done(updater, "受け取ったよ", {"spoke": found.speaks, "face": found.face})

    def _hold(self, event: dict, now: datetime | None = None) -> None:
        """速い道で使えるように、押されてきたものを手元に置く（docs/architecture.md の「声」）。"""
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
            # 常に録らない。MCP で入れたときだけ開ける（docs/architecture.md の「声」）
            self.session.set_listening(event["on"])

    def _react(self, found: events.Reaction) -> None:
        if self.session is None:
            return
        try:
            self.session.announce(found.text if found.speaks else "", found.face)
        except Exception:
            # 喋れなくても本体の仕事は終わっている。落ちた理由だけ残す
            log.exception("知らせを声に出せませんでした")


async def background(executor: Executor) -> None:
    """A2A サーバーと同じプロセスで、声でも話す。鍵が無い機械でも落とさない（`VoiceSession._talk` が握りつぶす）。

    マイクを開けるかは、MCP の「聞く（Mac のマイク）」で保存したもので始める（既定は切）。
    """
    session = VoiceSession(executor.held, config=executor.config)
    executor.session = session
    listening = (executor.records.get(SWITCH, LISTEN_KEY) or {}).get("on") is True
    try:
        await session.run(listening=listening)
    finally:
        executor.session = None
