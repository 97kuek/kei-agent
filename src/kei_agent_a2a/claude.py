"""エージェントが自分の claude を1回動かす（大学・研究で共通）。docs/agents.md

どのエージェントも、同じやり方で claude を動かす。

- 柵は `config.toml` から組む（sandbox、読ませない場所、許可した接続先）。作るのは `kei_agent.guard`
- 指示書は `prompts/<agent>.md`、作業場・上限時間・モデル・MCP は `Workspace` に載せて渡す
- 途中の経過（使った道具と、返答の断片）はタスクの状態に流す。オーケストレーターが1行に出す
- 返事は共通の封筒（`envelope.py`）。`data` には `RunResult` をそのまま入れる
- 上限（レートリミット）に当たったら `limit_reset_at` を載せて返す。やり直しの約束は本体が持つ

"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict

from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState

from kei_agent import guard, runner
from kei_agent.config import Config
from kei_agent.themes import Workspace
from kei_agent_a2a import envelope

log = logging.getLogger(__name__)

# 経過1つの長さの上限（タスクの記録が長くなりすぎないように）
PROGRESS_LIMIT = 800
# 止めたあと、プロセスが消えるのを待つ秒数
EXIT_GRACE_SECONDS = 5
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


# アカウントに付いている連携（claude.ai のコネクタ）を使う

# 連携の道具は、ユーザー設定を読み込まないと見えない。そのかわり、危ないものは名指しで断る
# 連携を使うだけなので、手元のファイルにも外の Web にも触らせない
DENY_ALWAYS = ("Bash", "Read", "Glob", "Grep", "Write", "Edit", "NotebookEdit",
               "WebFetch", "WebSearch", "Task")


async def ask_connector(config: Config, prompt: str, allowed: Sequence[str],
                        deny: Sequence[str] = (), timeout_minutes: int = 3) -> str:
    """アカウントの連携を、道具を絞って使わせる（返事の文をそのまま返す）。

    会社の Microsoft 365 のように、Claude のアカウントに付いている連携は、ユーザー設定を
    読み込まないと claude から見えない。そこでここだけ `--setting-sources user` を使い、
    **使ってよい道具を名指しで並べる**（Bash や書き込み、送信の道具は断る）。

    柵の作り方がほかと違うので、使うのはこの関数だけにする（docs/agents.md）。
    """
    command = [
        config.claude_bin, "-p", "--output-format", "text",
        # 連携はアカウント側にあるので、ユーザー設定を読み込む必要がある
        "--setting-sources", "user",
        "--permission-mode", "dontAsk",
        "--allowedTools", *allowed,
        "--disallowedTools", *DENY_ALWAYS, *deny,
    ]
    proc = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # ほかのドメインの鍵（Slack・Notion・Box・Moodle など）を、会社の claude に渡さない
        env=guard.strip_env(dict(os.environ)),
        # 上限時間で止めるとき、中で動いているものもまとめて止められるようにする
        start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout_minutes * 60)
    except TimeoutError:
        await _stop(proc)
        raise ConnectorError(f"連携の返事が {timeout_minutes} 分で返りませんでした") from None
    if proc.returncode:
        raise ConnectorError(f"連携を使えませんでした: {err.decode('utf-8', 'replace').strip()[:300]}")
    return out.decode("utf-8", "replace").strip()


async def _stop(proc) -> None:
    """claude と、その中で動いているものを止めて、後始末まで待つ。"""
    with suppress(OSError, ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)


def json_reply(text: str) -> list[dict]:
    """連携に JSON で答えさせたときの、返事の読み取り（前後に文が付いていても拾う）。

    「接続が拒否されました」のような文を「予定0件」と取り違えないよう、JSON の配列が
    見つからなければ断る。前置きに角括弧があっても、後ろから順に読めるものを探す。
    """
    starts = [m.start() for m in re.finditer(r"\[", text or "")]
    for start in reversed(starts):
        for end in reversed([m.end() for m in re.finditer(r"\]", text or "") if m.end() > start]):
            try:
                found = json.loads(text[start:end])
            except ValueError:
                continue
            if isinstance(found, list):
                return [item for item in found if isinstance(item, dict)]
    raise ConnectorError(f"連携の返事を読めません（JSON の配列がありません）: {(text or '')[:200]}")


async def finish(updater: TaskUpdater, payload: str) -> None:
    """封筒を見て、A2A のタスクを終わらせる。`ok: false` なら failed にする。

    うまくいかなかったのに completed で返すと、頼んだ側は失敗に気づけない。
    """
    try:
        ok = bool(json.loads(payload).get("ok"))
    except (ValueError, AttributeError):
        ok = False
    message = updater.new_agent_message([Part(text=payload)])
    await (updater.complete(message) if ok else updater.failed(message))


class ConnectorError(RuntimeError):
    pass
