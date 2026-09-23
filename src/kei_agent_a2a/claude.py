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
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState

from kei_agent import guard, runner
from kei_agent.agent_policy import policy_for
from kei_agent.codex_app_server import AppServerClient
from kei_agent.config import Config
from kei_agent.model_policy import ModelPolicyError, ResolvedModel, UseCase, resolve_selected
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


def ask_prompt(text: str) -> str:
    """自由な質問（`ask`）の本文。

    オーケストレーターは、スレッドの続きのために `session_id` や `channel` を添えた JSON で
    渡してくる。そのまま claude に流すと、質問がチャンネル ID ごと JSON に埋もれてしまうので、
    `prompt` だけを取り出す。素の文で届いたときは、そのまま質問として扱う。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return stripped
    try:
        ask = json.loads(stripped)
    except ValueError:
        return stripped
    if isinstance(ask, dict) and str(ask.get("prompt") or "").strip():
        return str(ask["prompt"]).strip()
    return stripped


async def progress(updater: TaskUpdater, payload: dict) -> None:
    """途中の様子を、タスクの状態に流す。"""
    short = {k: str(v)[:PROGRESS_LIMIT] for k, v in payload.items()}
    await updater.update_status(
        TaskState.TASK_STATE_WORKING,
        message=updater.new_agent_message([Part(text=json.dumps(short, ensure_ascii=False))]))


async def run(config: Config, ws: Workspace, ask: dict, updater: TaskUpdater,
              recipe: ResolvedModel) -> str:
    """用途別 recipe を確定済みの agent 実行を、封筒にして返す。"""
    assert ws.cwd is not None

    async def on_activity(activity: str) -> None:
        await progress(updater, {"activity": activity})

    async def on_text(chunk: str) -> None:
        await progress(updater, {"text": chunk})

    log.info("claude を動かします: %s（%s）", ws.channel_name, ws.cwd)
    result = await runner.run_model(
        config,
        runner.ExecutionRequest(
            ws, recipe, ask.get("session_id"), ask.get("channel", ""), ask.get("thread_ts", ""),
            read_only=bool(ask.get("read_only")),
        ),
        ask["prompt"], on_activity=on_activity, on_text=on_text,
    )
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


def connector_command(config: Config, allowed: Sequence[str], deny: Sequence[str],
                      plugin_dir: Path, recipe: ResolvedModel | None = None) -> list[str]:
    """連携を使う claude の起動コマンド。

    `plugin_dir` はそのエージェントの skill の置き場（`plugin/<agent>/`）。`plugin/` そのものを
    渡すと中の plugin を全部読んでしまうので、必ず1つぶんを名指しする。skill を呼べるように
    `Skill` を許可の一覧に足すが、外部の道具は呼び出し元が並べたものだけにする。
    """
    command = [
        config.claude_bin, "-p", "--output-format", "text",
        # 連携はアカウント側にあるので、ユーザー設定を読み込む必要がある
        "--setting-sources", "user",
        "--permission-mode", "dontAsk",
        "--plugin-dir", str(plugin_dir),
        "--allowedTools", *allowed, "Skill",
        "--disallowedTools", *DENY_ALWAYS, *deny,
    ]
    if recipe is not None:
        command += ["--model", recipe.model]
        if recipe.reasoning_effort:
            command += ["--effort", recipe.reasoning_effort]
    return command


async def ask_codex_app(config: Config, agent: str, prompt: str, model: str,
                        reasoning_effort: str, timeout_minutes: int) -> str:
    """接続済み Codex App を agent policy の範囲だけで使う。"""
    result = await AppServerClient(config.codex_bin, timeout_minutes * 60).run(
        prompt, policy_for(agent), model, reasoning_effort)
    if result.is_error:
        # App Server の内部エラーや外部データは Slack へ出さない。
        raise ConnectorError("Codex の接続を使えませんでした")
    return result.text


async def ask_connector(config: Config, prompt: str, allowed: Sequence[str], plugin_dir: Path,
                        deny: Sequence[str] = (), timeout_minutes: int = 3, *, store=None,
                        agent: str = "", use_case: UseCase | None = None) -> str:
    """アカウントの連携を、道具を絞って使わせる（返事の文をそのまま返す）。

    会社の Microsoft 365 のように、Claude のアカウントに付いている連携は、ユーザー設定を
    読み込まないと claude から見えない。そこでここだけ `--setting-sources user` を使い、
    **使ってよい道具を名指しで並べる**（Bash や書き込み、送信の道具は断る）。

    柵の作り方がほかと違うので、使うのはこの関数だけにする（docs/agents.md）。
    """
    recipe = None
    if store is not None and agent:
        default_cases = {"course": UseCase.COURSE_EXPLAIN, "work": UseCase.WORK_SINGLE_SOURCE}
        try:
            recipe = resolve_selected(config, store, agent, use_case or default_cases[agent])
        except (KeyError, ModelPolicyError) as e:
            raise ConnectorError(str(e)) from e
    if recipe is not None and recipe.provider == "codex":
        # Codex の失敗を Claude で再試行すると、利用者が選んだ provider と権限境界を破る。
        return await ask_codex_app(config, agent, prompt, recipe.model, recipe.reasoning_effort, timeout_minutes)

    command = connector_command(config, allowed, deny, plugin_dir, recipe)
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

    後ろに出典（`[1, 2]`）のような別の配列が付くことがあるので、**中身のある配列を優先**する。
    先に見つけた `[1, 2]` を返すと、予定があるのに0件として返してしまう。
    """
    text = text or ""
    starts = [m.start() for m in re.finditer(r"\[", text)]
    ends = [m.end() for m in re.finditer(r"\]", text)]
    empty = None
    for start in reversed(starts):
        for end in reversed([e for e in ends if e > start]):
            try:
                found = json.loads(text[start:end])
            except ValueError:
                continue
            if not isinstance(found, list):
                continue
            items = [item for item in found if isinstance(item, dict)]
            if items:
                return items
            if empty is None:
                empty = items
    if empty is not None:
        return empty
    raise ConnectorError(f"連携の返事を読めません（JSON の配列がありません）: {text[:200]}")


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
