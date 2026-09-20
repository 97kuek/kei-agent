"""エージェントが自分の claude を1回動かす（大学・研究で共通）。docs/agents.md

どのエージェントも、同じやり方で claude を動かす。

- 柵は `config.toml` から組む（sandbox、読ませない場所、許可した接続先）。作るのは `kei_agent.guard`
- 指示書は `prompts/<agent>.md`、作業場・上限時間・モデル・MCP は `Workspace` に載せて渡す
- 途中の経過（使った道具と、返答の断片）はタスクの状態に流す。オーケストレーターが1行に出す
- 返事は共通の封筒（`envelope.py`）。`data` には `RunResult` をそのまま入れる
- 上限（レートリミット）に当たったら `limit_reset_at` を載せて返す。やり直しの約束は本体が持つ

MCP の設定にはトークンが入るので、sandbox から読めない場所（`<state_dir>/secrets/`）に置き、
使い終わったら消す。claude は MCP の道具としては使えるが、トークンの文字列は読めない。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState

from kei_agent import runner
from kei_agent.config import Config
from kei_agent.themes import Workspace
from kei_agent_a2a import envelope

log = logging.getLogger(__name__)

# 経過1つの長さの上限（タスクの記録が長くなりすぎないように）
PROGRESS_LIMIT = 800
NO_PROMPT = "依頼の JSON に prompt が要ります"


def ask_json(text: str) -> dict:
    """届いた依頼（JSON）を辞書にする。prompt が無ければ断る。"""
    try:
        ask = json.loads(text)
    except ValueError:
        raise ValueError(NO_PROMPT) from None
    if not isinstance(ask, dict) or not str(ask.get("prompt") or "").strip():
        raise ValueError(NO_PROMPT)
    return ask


def clean_mcp_configs(state_dir: Path, name: str) -> int:
    """前に強制終了して残った MCP の設定を消す（トークンが書いてあるので残さない）。"""
    directory = state_dir / "secrets"
    removed = 0
    for path in directory.glob(f"mcp-{name}-*.json"):
        path.unlink(missing_ok=True)
        removed += 1
    if removed:
        log.info("残っていた MCP の設定を %d 個片づけました", removed)
    return removed


@contextmanager
def mcp_config(state_dir: Path, name: str, servers: dict) -> Iterator[Path | None]:
    """MCP の設定を、sandbox から読めない場所に置く（使い終わったら消す）。"""
    if not servers:
        yield None
        return
    directory = state_dir / "secrets"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / f"mcp-{name}-{os.getpid()}.json"
    path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


async def progress(updater: TaskUpdater, payload: dict) -> None:
    """途中の様子を、タスクの状態に流す。"""
    short = {k: str(v)[:PROGRESS_LIMIT] for k, v in payload.items()}
    await updater.update_status(
        TaskState.TASK_STATE_WORKING,
        message=updater.new_agent_message([Part(text=json.dumps(short, ensure_ascii=False))]))


async def run(config: Config, ws: Workspace, ask: dict, updater: TaskUpdater) -> str:
    """claude を1回動かして、封筒（JSON 文字列）を返す。"""
    assert ws.cwd is not None

    async def on_activity(activity: str) -> None:
        await progress(updater, {"activity": activity})

    async def on_text(chunk: str) -> None:
        await progress(updater, {"text": chunk})

    log.info("claude を動かします: %s（%s）", ws.channel_name, ws.cwd)
    result = await runner.run_claude(
        config, ws, ask["prompt"], ask.get("session_id"), ask.get("channel", ""),
        ask.get("thread_ts", ""), on_activity, on_text)
    log.info("claude が終わりました: %s（エラー: %s）", ws.channel_name, result.is_error)
    return envelope.reply(
        text=result.text,
        data=asdict(result),
        ok=not result.is_error,
        limit_reset_at=result.limit_reset_at,
        cost_usd=result.cost_usd,
    )
