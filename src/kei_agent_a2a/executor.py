"""頼まれた仕事をこなすところの土台（大学・研究・仕事・モジュールのエージェントで共通）。

A2A では、相手からのメッセージは `RequestContext` に入って届き、結果は `EventQueue` に流す。
ここは「仕事を受け付けたと知らせ、本文と metadata を取り出して `handle` に渡す」までと、
どのエージェントでも同じ形の自由な依頼（`ask`）。定型の仕事は、エージェントごとの `handle` が決める。
"""

from __future__ import annotations

import json
import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from kei_agent.configuration.config import Config, load_config
from kei_agent.execution.model_classifier import UsageLimited
from kei_agent.execution.model_policy import ModelPolicyError
from kei_agent.storage.records import Records
from kei_agent.storage.store import Store
from kei_agent.workspaces import themes
from kei_agent.workspaces.themes import Workspace
from kei_agent_a2a import envelope, run

log = logging.getLogger(__name__)

# metadata の days で受け付ける上限（日）
MAX_DAYS = 400
# 定型に当てはまらない依頼の窓口（どのエージェントでも同じ名前）
ASK = "ask"


def message_text(context: RequestContext) -> str:
    """届いたメッセージの本文（text の Part をつないだもの）。"""
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", []) if message is not None else []
    return "\n".join(p.text for p in parts if getattr(p, "text", ""))


def asked_days(metadata: dict | None, default: int, maximum: int = MAX_DAYS) -> int:
    """metadata の days（何日先まで／何日ぶん）。数字でないか範囲の外なら既定のまま。"""
    try:
        days = int((metadata or {}).get("days", default))
    except (TypeError, ValueError):
        return default
    return days if 1 <= days <= maximum else default


# 材料が本文の JSON でないときの断り
NO_JSON = "材料は本文の JSON で渡してください"


def body_json(text: str) -> dict:
    """本文の JSON（本体の core.ask_agent が材料を渡す形）。辞書でなければ ValueError（NO_JSON）。"""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        raise ValueError(NO_JSON) from None
    if not isinstance(data, dict):
        raise ValueError(NO_JSON)
    return data


def provider_of(metadata: dict) -> str:
    """本体が metadata で指定した provider（claude / codex。無ければ空文字で、担当の既定）。"""
    return str(metadata.get("provider") or "")


class SkillExecutor(AgentExecutor):
    """仕事を受け付けて `handle` に渡す。返事は全エージェント共通の封筒（`envelope.py`）。

    使う側は `agent`（制限の表の名前）を持つ。設定（`config`）と保存（`store`）は、渡さなければここで用意する。
    終わったら `done`、断るなら `fail`、自由な依頼は `answer` に渡す。
    """

    agent = ""

    def __init__(self, config: Config | None = None, store: Store | None = None):
        self.config = config or load_config()
        self.store = store or Store(self.config.db_path)

    @property
    def records(self) -> Records:
        """このモジュールだけの記録（本体側の core.records と同じもの。App Home のオン・オフなども読める）。"""
        return Records(self.store, self.agent)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = dict(getattr(context, "metadata", None) or {})
        text = message_text(context)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # 仕事の状態を知らせる前に、まず「その仕事がある」ことを相手に渡す（A2A の決まり）
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await updater.start_work()
        await self.handle(updater, metadata, text)

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        raise NotImplementedError

    async def pick(self, updater: TaskUpdater, metadata: dict, text: str, names: tuple[str, ...] | list[str],
                   default: str) -> str | None:
        """頼まれた仕事の名前（metadata の skill。無ければ default）。

        できない仕事なら断り、自由な依頼（ask）なら answer に渡して、どちらも None を返す（呼んだ側は何もしない）。
        """
        skill = str(metadata.get("skill") or default)
        if skill not in names:
            await self.fail(updater, f"できるのは {' / '.join(names)} だけです")
            return None
        if skill == ASK:
            await self.answer(updater, text)
            return None
        return skill

    def workspace(self, ask: dict) -> Workspace:
        """自由な依頼で AI を動かす作業場。研究はテーマごとに変える（上書きする）。"""
        return themes.agent_workspace(self.config, self.agent)

    async def answer(self, updater: TaskUpdater, text: str) -> None:
        """自由な依頼（`ask`）。依頼も返事も、どのエージェントでも同じ形（`kei_agent_a2a.run`）。"""
        try:
            ask = run.ask_json(text)
            ws = self.workspace(ask)
            recipe = await run.recipe_for(self.config, self.store, self.agent, ask)
        except UsageLimited as e:
            await self.fail(updater, str(e), e.reset_at)
            return
        except (ValueError, ModelPolicyError) as e:
            await self.fail(updater, str(e))
            return
        await run.finish(updater, await run.execute(self.config, ws, ask, updater, recipe))

    async def fail(self, updater: TaskUpdater, reason: str, limit_reset_at: float | None = None) -> None:
        log.warning("断りました: %s", reason)
        await updater.failed(updater.new_agent_message([
            Part(text=envelope.reply(reason, ok=False, limit_reset_at=limit_reset_at))]))

    async def done(self, updater: TaskUpdater, text: str, data: dict | None = None) -> None:
        await run.finish(updater, envelope.reply(text, data))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
