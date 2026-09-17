"""claude -p の起動と、stream-json の読み取り。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ezra.config import Config
from ezra.themes import ChannelKind, Workspace

# claude -p の子プロセスに渡さない環境変数。Bash から Slack や Notion のトークンが見えないようにする
_STRIPPED_ENV_PREFIXES = ("SLACK_", "NOTION_", "EZRA_ALLOWED_", "CLAUDECODE", "CLAUDE_CODE_")
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
            "filesystem": {"allowWrite": [str(p) for p in config.allow_write]},
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
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_claude(
    config: Config,
    ws: Workspace,
    prompt: str,
    session_id: str | None,
    channel: str,
    thread_ts: str,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
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
            activity = apply_event(result, event)
            if activity and on_activity:
                await on_activity(activity)

    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        await asyncio.wait_for(read_stdout(), timeout=config.run_timeout_minutes * 60)
        await proc.wait()
    except TimeoutError:
        _kill_group(proc.pid)
        await proc.wait()
        result.timed_out = True
        result.is_error = True
    stderr = (await stderr_task).decode("utf-8", "replace").strip()
    if proc.returncode and not result.text and not result.errors and stderr:
        result.is_error = True
        result.errors.append(stderr[-2000:])
    return result
