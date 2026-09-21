"""頼まれた作業（claude を1回動かす／長い処理をジョブにする）をこなすところ。

できるのは2つ。claude を1回動かすことと、長い処理（pueue のジョブ）の出し入れ。依頼は JSON で届く。

    run-claude  {"channel_name": "amr-query", "prompt": "図を作って", "session_id": null,
                 "channel": "C1", "thread_ts": "1.2", "allowed_domains": ["example.com"]}
    submit-job  {"cwd": "~/research/amr-query", "command": "uv run x.py", "label": "kei-agent-3"}
    list-jobs   {}
    cancel-job / forget-job  {"task_id": 12}

ジョブが「どのスレッドのものか」「できるはずのファイルは何か」は、オーケストレーターが覚えている。
ここは pueue の待ち行列を持つだけ（docs/design.md の11章）。

返すのは全エージェント共通の封筒（`kei_agent_a2a/envelope.py`）で、`data` には `RunResult` が入る。
経過と柵の扱いは `kei_agent_a2a/claude.py`（大学エージェントと共通）。

会話の続け方（session の付け替え、履歴の戻し）と Slack への見せ方は持たない。
それはオーケストレーターの仕事（docs/design.md の11章）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent import themes
from kei_agent.config import Config, load_config
from kei_agent.jobs import Pueue
from kei_agent_a2a import claude, envelope
from kei_agent_research.card import CANCEL_JOB, FORGET_JOB, LIST_JOBS, RUN_CLAUDE, SUBMIT_JOB

log = logging.getLogger(__name__)

SKILLS = (RUN_CLAUDE, SUBMIT_JOB, LIST_JOBS, CANCEL_JOB, FORGET_JOB)
NO_JSON = "依頼は JSON で渡してください"


def _json(text: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        raise ValueError(NO_JSON) from None
    if not isinstance(data, dict):
        raise ValueError(NO_JSON)
    return data


def message_text(context: RequestContext) -> str:
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


class ResearchExecutor(AgentExecutor):
    def __init__(self, config: Config | None = None, pueue: Pueue | None = None):
        self.config = config or load_config()
        self.pueue = pueue or Pueue(self.config)
        # pueue のグループは最初に使うときだけ用意する
        self._group_ready = False

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        text = message_text(context)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        skill = metadata.get("skill", RUN_CLAUDE)
        if skill not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} です")
            return
        try:
            ask = claude.ask_json(text) if skill == RUN_CLAUDE else _json(text)
        except ValueError as e:
            await self._fail(updater, str(e))
            return
        if skill == RUN_CLAUDE:
            await self._run_claude(updater, ask)
            return
        await self._job(updater, skill, ask)

    async def _run_claude(self, updater: TaskUpdater, ask: dict) -> None:
        try:
            ws = themes.resolve(self.config, str(ask.get("channel_name") or ""))
        except ValueError as e:
            await self._fail(updater, str(e))
            return
        if ws.cwd is None:
            await self._fail(updater, f"#{ws.channel_name} には作業用ディレクトリがありません")
            return
        ws = replace(ws, allowed_domains=tuple(ask.get("allowed_domains") or ()))
        themes.ensure_workspace(ws)
        # claude が失敗したときは、封筒の ok が false になる。A2A のタスクも failed にする
        await claude.finish(updater, await claude.run(self.config, ws, ask, updater))

    async def _job(self, updater: TaskUpdater, skill: str, ask: dict) -> None:
        """長い処理（pueue のジョブ）。どのスレッドのジョブかはオーケストレーターが覚えている。"""
        try:
            if skill == SUBMIT_JOB:
                cwd = self._theme_dir(str(ask.get("cwd") or ""))
                if not self._group_ready:
                    await self.pueue.ensure_group()
                    self._group_ready = True
                task_id = await self.pueue.add(cwd, str(ask.get("command") or ""),
                                               label=str(ask.get("label") or ""))
                await self._done(updater, f"ジョブを入れました（pueue {task_id}）", {"task_id": task_id})
            elif skill == LIST_JOBS:
                tasks = await self.pueue.tasks()
                await self._done(updater, f"動いているジョブ: {len(tasks)} 件",
                                 {"tasks": {str(k): v for k, v in tasks.items()}})
            elif skill == CANCEL_JOB:
                await self.pueue.kill(int(ask["task_id"]))
                await self._done(updater, f"ジョブを止めました（pueue {ask['task_id']}）")
            else:
                await self.pueue.remove(int(ask["task_id"]))
                await self._done(updater, f"ジョブを片づけました（pueue {ask['task_id']}）")
        except (KeyError, TypeError, ValueError) as e:
            await self._fail(updater, f"ジョブの依頼が読めません: {e}")
        except RuntimeError as e:
            await self._fail(updater, f"pueue が失敗しました: {e}")

    def _theme_dir(self, cwd: str) -> Path:
        """研究テーマのディレクトリの中だけを受け付ける（渡された場所で何でも動かさない）。"""
        root = self.config.research_root.resolve()
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir() or root not in path.parents:
            raise ValueError(f"研究のディレクトリの中ではありません: {cwd}")
        return path

    async def _done(self, updater: TaskUpdater, text: str, data: dict | None = None) -> None:
        await claude.finish(updater, envelope.reply(text, data))

    async def _fail(self, updater: TaskUpdater, reason: str) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([Part(text=envelope.failure(reason))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
