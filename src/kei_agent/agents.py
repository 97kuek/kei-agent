"""ほかのエージェントに仕事を頼む（オーケストレーター側の共通部分）。docs/agents.md

住所は `config.toml` の `[a2a.agents]`（名前 = 住所）。返事は全エージェント共通の封筒
（`kei_agent_a2a/envelope.py`）で受け取る。どのエージェントでも同じように頼み、同じように読む。

上限（レートリミット）に当たったことは封筒の `limit_reset_at` で返ってくる。依頼者への約束
（「◯時ごろに自動でやり直す」）はオーケストレーターが1か所で持つ（docs/design.md の11章）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kei_agent import a2a
from kei_agent.config import Config

log = logging.getLogger(__name__)

# claude を動かす仕事は、返事までに claude の上限時間がかかる
CLAUDE_SKILLS = ("run-claude", "ask")
# 相手が入れ替わっている最中につながらなかったときに、待ってやり直す回数と秒数
RETRIES, RETRY_WAIT = 1, 3.0


@dataclass
class Reply:
    """エージェントからの返事（封筒を開いたもの）。"""
    ok: bool = True
    text: str = ""
    data: dict = field(default_factory=dict)
    limit_reset_at: float | None = None
    cost_usd: float | None = None
    state: str = ""

    @classmethod
    def of(cls, task: a2a.TaskResult) -> Reply:
        """返ってきたタスクを封筒として読む。封筒でなければ、本文だけの返事として扱う。"""
        try:
            payload = json.loads(task.answer)
            if not isinstance(payload, dict) or "ok" not in payload:
                raise ValueError
        except ValueError:
            return cls(ok=task.ok, text=task.answer, state=task.state)
        return cls(
            ok=bool(payload.get("ok")) and task.ok,
            text=str(payload.get("text") or ""),
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            limit_reset_at=payload.get("limit_reset_at"),
            cost_usd=payload.get("cost_usd"),
            state=task.state,
        )

    @classmethod
    def broken(cls, why: str) -> Reply:
        return cls(ok=False, text=why)


def build(config: Config) -> dict[str, a2a.Agent]:
    """config.toml の [a2a.agents] から、頼む相手を用意する。"""
    agents: dict[str, a2a.Agent] = {}
    for name, url in config.a2a.agents.items():
        if not url:
            continue
        # claude を動かす仕事があるので、待つ時間は claude の上限時間を足しておく
        timeout = config.run_timeout_minutes * 60 + config.a2a.timeout_seconds
        agents[name] = a2a.Agent(url, config.a2a_token, timeout=timeout)
    return agents


async def ask(agent: a2a.Agent, skill: str, params: dict | None = None,
              on_progress: Callable[[str], Awaitable[None]] | None = None,
              text: str = "") -> Reply:
    """1つ頼んで、封筒を開いて返す。つながらなければ、その理由を入れた返事にする。

    経過を見せたい仕事（claude を動かすもの）は、流しながら受け取る。
    """
    stream = on_progress is not None or skill in CLAUDE_SKILLS
    log.info("エージェントに頼みます: %s に %s%s", agent.base_url, skill,
             f"（{params}）" if params else "")
    for left in reversed(range(RETRIES + 1)):
        try:
            if stream:
                return Reply.of(await agent.stream(skill, text or skill, params=params,
                                                   on_progress=on_progress))
            return Reply.of(await agent.ask(skill, text or skill, params=params))
        except a2a.NotReachable as e:
            # 入れ直しの最中は数秒つながらない。依頼者に失敗を見せる前に、待ってやり直す
            if not left:
                log.warning("%s を頼めませんでした: %s", skill, e)
                return Reply.broken(f"{skill} を頼めなかった: {e}")
            log.info("%s につながらないので %.0f 秒待ってやり直します", agent.base_url, RETRY_WAIT)
            await asyncio.sleep(RETRY_WAIT)
        except a2a.A2AError as e:
            log.warning("%s を頼めませんでした: %s", skill, e)
            return Reply.broken(f"{skill} を頼めなかった: {e}")
    raise AssertionError("ここには来ない")  # pragma: no cover
