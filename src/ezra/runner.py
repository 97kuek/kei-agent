"""claude -p の起動と、stream-json の読み取り。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from contextlib import suppress

from ezra.config import Config, path_without_venv
from ezra.themes import ChannelKind, Workspace

# claude が終わったあと、プロセスが消えるのを待つ秒数
EXIT_GRACE_SECONDS = 5

# claude -p の子プロセスに渡さない環境変数。Bash から Slack や Notion のトークンが見えないようにする
_STRIPPED_ENV_PREFIXES = ("SLACK_", "NOTION_", "EZRA_ALLOWED_", "CLAUDECODE", "CLAUDE_CODE_", "VIRTUAL_ENV")
_KEPT_CLAUDE_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)


def _abs_rule(tool: str, path: Path) -> str:
    # 権限ルールで絶対パスを書くときは // で始める
    return f"{tool}(/{path}/**)"


def build_settings(config: Config, ws: Workspace) -> dict:
    assert ws.cwd is not None
    read_root = config.research_root if ws.kind is ChannelKind.OVERVIEW else ws.cwd
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {
                "allowedDomains": list(config.allowed_domains),
                "strictAllowlist": True,
            },
            "filesystem": {
                "allowWrite": [str(p) for p in config.allow_write],
                # sandbox は既定で PC 全体を読めるので、秘密情報の置き場所を塞ぐ
                "denyRead": [str(p) for p in config.deny_read],
            },
        },
        "permissions": {
            "allow": [
                _abs_rule("Read", read_root),
                _abs_rule("Edit", ws.cwd),
                "Glob",
                "Grep",
                "Bash",
                "WebSearch",
                "WebFetch",
                "Skill",
                "TodoWrite",
            ],
        },
    }


def build_command(config: Config, ws: Workspace, session_id: str | None) -> list[str]:
    cmd = [
        config.claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        # ユーザー設定（フックやプラグイン、広い許可ルール）を持ち込まない
        "--setting-sources", "",
        "--settings", json.dumps(build_settings(config, ws), ensure_ascii=False),
        "--permission-mode", "dontAsk",
        "--plugin-dir", str(config.plugin_dir),
    ]
    if config.system_prompt_path.exists():
        cmd += ["--append-system-prompt", config.system_prompt_path.read_text(encoding="utf-8")]
    if config.model:
        cmd += ["--model", config.model]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def build_env(config: Config, base: dict[str, str], channel: str, thread_ts: str) -> dict[str, str]:
    env = {
        k: v for k, v in base.items()
        if k in _KEPT_CLAUDE_ENV or not k.startswith(_STRIPPED_ENV_PREFIXES)
    }
    env["PATH"] = path_without_venv(base.get("PATH", ""), config.repo_root)
    env["EZRA_CHANNEL"] = channel
    env["EZRA_THREAD_TS"] = thread_ts
    env["EZRA_PLUGIN_DIR"] = str(config.plugin_dir)
    return env


def describe_tool(name: str, tool_input: dict) -> str:
    """経過用メッセージに出す、ツール呼び出しの短い説明。"""
    def short(s: str, n: int = 80) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "Bash":
        return "Bash: " + short(tool_input.get("description") or tool_input.get("command", ""))
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return f"{name}: {short(tool_input.get('file_path', ''))}"
    if name in ("Glob", "Grep"):
        return f"{name}: {short(tool_input.get('pattern', ''))}"
    if name == "WebSearch":
        return "Web検索: " + short(tool_input.get("query", ""))
    if name == "WebFetch":
        return "Webページ取得: " + short(tool_input.get("url", ""))
    if name == "Skill":
        return "skill: " + short(tool_input.get("skill", ""))
    if name == "TodoWrite":
        return "作業計画を更新"
    return name


@dataclass
class RunResult:
    session_id: str | None = None
    text: str = ""
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    errors: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def session_missing(self) -> bool:
        return any("No conversation found" in e for e in self.errors)


def apply_event(result: RunResult, event: dict) -> str | None:
    """1行分のイベントを結果に反映する。経過に出す文があれば返す。"""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        result.session_id = event.get("session_id")
        return None
    if etype == "assistant":
        activity = None
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                activity = describe_tool(block.get("name", ""), block.get("input") or {})
                result.activities.append(activity)
        return activity
    if etype == "result":
        result.session_id = event.get("session_id") or result.session_id
        result.text = event.get("result") or ""
        result.is_error = bool(event.get("is_error"))
        result.cost_usd = event.get("total_cost_usd")
        result.duration_ms = event.get("duration_ms")
        result.errors = [str(e) for e in event.get("errors") or []]
    return None


def _kill_group(pid: int) -> None:
    """claude とその中で動いている Bash を、プロセスグループごと止める。

    start_new_session=True で起動しているので、グループIDは claude の pid と同じ。
    claude 本体を回収したあとでも、残った子プロセスを止められるよう getpgid は使わない。
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_claude(
    config: Config,
    ws: Workspace,
    prompt: str,
    session_id: str | None,
    channel: str,
    thread_ts: str,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
) -> RunResult:
    assert ws.cwd is not None
    proc = await asyncio.create_subprocess_exec(
        *build_command(config, ws, session_id),
        cwd=ws.cwd,
        env=build_env(config, dict(os.environ), channel, thread_ts),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        # 上限時間で止めるとき、中で動いている Bash などもまとめて止められるよう、別のプロセスグループにする
        start_new_session=True,
    )
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()

    result = RunResult()

    async def read_stdout() -> None:
        async for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if on_text and event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and (block.get("text") or "").strip():
                        await on_text(block["text"])
            activity = apply_event(result, event)
            if activity and on_activity:
                await on_activity(activity)
            if event.get("type") == "result":
                # claude の最後のイベント。ここで読むのをやめる。
                # Bash が残したプロセスが出力を握っていると、EOF はいつまでも来ない
                return

    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        await asyncio.wait_for(read_stdout(), timeout=config.run_timeout_minutes * 60)
    except TimeoutError:
        result.timed_out = True
        result.is_error = True
    except ValueError:
        # stream-json の1行が limit を超えた
        result.is_error = True
        result.errors.append("claude の出力が大きすぎて読めませんでした")
    finally:
        if not result.timed_out:
            # claude がセッションを保存して終わるのを、少しだけ待つ
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)
        # 正常に終わっていれば空振りする。Bash が残したプロセスがいれば、ここでまとめて止める
        _kill_group(proc.pid)
        stderr = await _drain(stderr_task)
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)
    if proc.returncode and not result.text and not result.errors and stderr:
        result.is_error = True
        result.errors.append(stderr[-2000:])
    return result


async def _drain(task: asyncio.Task) -> str:
    """stderr を待つ。子プロセスが握ったままでも、待ち続けない。"""
    try:
        return (await asyncio.wait_for(task, timeout=EXIT_GRACE_SECONDS)).decode("utf-8", "replace").strip()
    except (TimeoutError, asyncio.CancelledError, ValueError):
        return ""
