"""ほかのエージェントに仕事を頼む（オーケストレーター側の共通部分）。docs/architecture.md の「振り分けと A2A」

住所は `config.toml` の `[a2a.agents]`（名前 = 住所）。返事は全エージェント共通の封筒
（`kei_agent_a2a/envelope.py`）で受け取る。どのエージェントでも同じように頼み、同じように読む。

上限（レートリミット）に当たったことは封筒の `limit_reset_at` で返ってくる。依頼者への約束
（「◯時ごろに自動でやり直す」）はオーケストレーターが1か所で持つ（docs/architecture.md）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kei_agent import a2a, runner
from kei_agent.config import Config

log = logging.getLogger(__name__)

# provider を動かす仕事（どのエージェントでも同じ名前）。返事までに AI の上限時間がかかる
ASK = "ask"
MODEL_SKILLS = (ASK,)
# `ask` の返事（封筒の data）のうち、受け取る RunResult の項目（知らない項目が増えても落ちないように、ここで絞る）
FIELDS = ("session_id", "text", "is_error", "cost_usd", "duration_ms", "errors", "activities",
          "timed_out", "requested_domains", "limit_reset_at", "provider")
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
        # AI を動かす仕事があるので、待つ時間は AI の上限時間を足しておく
        timeout = config.run_timeout_minutes * 60 + config.a2a.timeout_seconds
        agents[name] = a2a.Agent(url, config.a2a_token, timeout=timeout)
    return agents


async def ask(agent: a2a.Agent, skill: str, params: dict | None = None,
              on_progress: Callable[[str], Awaitable[None]] | None = None,
              text: str = "") -> Reply:
    """1つ頼んで、封筒を開いて返す。つながらなければ、その理由を入れた返事にする。

    経過を見せたい仕事（AI を動かすもの）は、流しながら受け取る。
    """
    stream = on_progress is not None or skill in MODEL_SKILLS
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


def to_result(data: dict) -> runner.RunResult:
    """封筒の `data` を RunResult に戻す。"""
    result = runner.RunResult(**{k: v for k, v in data.items() if k in FIELDS})
    # JSON では組が配列になるので、戻しておく（接続先の許可を聞くときに使う）
    result.requested_domains = [(d[0], d[1]) for d in result.requested_domains if len(d) >= 2]
    return result


async def run_ask(agent: a2a.Agent, payload: dict,
                  on_activity: Callable[[str], Awaitable[None]] | None = None) -> runner.RunResult:
    """どのエージェントにも同じ形で `ask` を頼み、実行結果で受け取る（依頼の形は kei_agent_a2a.run）。"""

    async def on_progress(raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if on_activity and isinstance(event, dict) and event.get("activity"):
            await on_activity(event["activity"])

    reply = await ask(agent, ASK, on_progress=on_progress, text=json.dumps(payload, ensure_ascii=False))
    if not reply.data:
        # 封筒が開けなかった（つながらない、途中で切れた、形が違う）か、動かす前に断られた（上限など）
        return runner.RunResult(is_error=True, errors=[reply.text or "エージェントが返事をしませんでした"],
                                limit_reset_at=reply.limit_reset_at,
                                provider=str(payload.get("provider") or "") or None)
    return to_result(reply.data)
