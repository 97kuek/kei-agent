"""claude -p の起動と、stream-json の読み取り。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kei_agent import guard
from kei_agent.config import Config, path_without_venv
from kei_agent.themes import Workspace

# 契約の上限に達したときに claude -p が返す文。書き方は版によって違う。
#   `Claude AI usage limit reached|<エポック秒>`
#   `You've hit your session limit · resets 6:30pm (Asia/Tokyo)`
#   `5-hour limit reached ∙ resets 3pm`
# 明ける時刻が古いまま返ることがあるので、過去の時刻はそのまま使わない
_USAGE_LIMIT = re.compile(r"(?:usage|session|hour|weekly)\s+limit\s+reached|hit your (?:usage|session|\w+)\s*limit",
                          re.IGNORECASE)
_LIMIT_EPOCH = re.compile(r"limit reached\|(\d{10,13})")
_LIMIT_RESETS = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
# 明ける時刻が分からないときに、これだけ待ってからやり直す（秒）
UNKNOWN_LIMIT_RESET = -1.0

# claude が終わったあと、プロセスが消えるのを待つ秒数
EXIT_GRACE_SECONDS = 5

def system_prompt_text(config: Config) -> str:
    path = config.system_prompt_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def system_prompt_version(config: Config) -> str:
    """prompts/system.md の版。--resume では会話を始めたときの版が使われ続けるので、変わったかを見るのに使う。"""
    return hashlib.sha256(system_prompt_text(config).encode("utf-8")).hexdigest()[:12]


def run_timeout_seconds(config: Config, ws: Workspace) -> float:
    """claude 1回の上限時間（秒）。ワークスペースの指定があれば、そちらを使う。"""
    return (ws.timeout_minutes or config.run_timeout_minutes) * 60


def build_command(config: Config, ws: Workspace, session_id: str | None) -> list[str]:
    cmd = [
        config.claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        # ユーザー設定（フックやプラグイン、広い許可ルール）を持ち込まない
        "--setting-sources", "",
        "--settings", json.dumps(guard.build_settings(config, ws), ensure_ascii=False),
        "--permission-mode", "dontAsk",
        "--plugin-dir", str(config.plugin_dir),
    ]
    prompt_path = ws.system_prompt or config.system_prompt_path
    if prompt_path.exists():
        # --resume のときは効かない（会話を始めたときの版が残る）。変わった版は assistant が本文で渡す
        cmd += ["--append-system-prompt", prompt_path.read_text(encoding="utf-8")]
    model = ws.model or config.model
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def build_env(config: Config, base: dict[str, str], channel: str, thread_ts: str) -> dict[str, str]:
    env = guard.strip_env(base)
    env["PATH"] = path_without_venv(base.get("PATH", ""), config.repo_root)
    env["KEI_AGENT_CHANNEL"] = channel
    env["KEI_AGENT_THREAD_TS"] = thread_ts
    env["KEI_AGENT_PLUGIN_DIR"] = str(config.plugin_dir)
    return env


def describe_tool(name: str, tool_input: dict) -> str:
    """経過用メッセージに出す、ツール呼び出しの短い説明。"""
    def short(s: str, n: int = 80) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "Bash":
        return "実行している: " + short(tool_input.get("description") or tool_input.get("command", ""))
    if name == "Read":
        return f"読んでいる: {short(tool_input.get('file_path', ''))}"
    if name in ("Write", "NotebookEdit"):
        return f"書いている: {short(tool_input.get('file_path', ''))}"
    if name == "Edit":
        return f"直している: {short(tool_input.get('file_path', ''))}"
    if name == "Glob":
        return f"探している: {short(tool_input.get('pattern', ''))}"
    if name == "Grep":
        return f"調べている: {short(tool_input.get('pattern', ''))}"
    if name == "WebSearch":
        return "Web で検索している: " + short(tool_input.get("query", ""))
    if name == "WebFetch":
        return "Web ページを読んでいる: " + short(tool_input.get("url", ""))
    if name == "Skill":
        return "skill を使っている: " + short(tool_input.get("skill", ""))
    if name == "TodoWrite":
        return "作業の進め方を整理している"
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
    # Bash の allowed_domains で広げようとした接続先と、そのときの説明。sandbox では断られるので、
    # Kei Agent が依頼者に [許可する] [断る] を聞く（docs/design.md の9章）
    requested_domains: list[tuple[str, str]] = field(default_factory=list)
    # 契約の上限に達したときの、明ける時刻（エポック秒）。分からないときは UNKNOWN_LIMIT_RESET
    limit_reset_at: float | None = None

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
                tool_input = block.get("input") or {}
                activity = describe_tool(block.get("name", ""), tool_input)
                result.activities.append(activity)
                known = {d for d, _ in result.requested_domains}
                for domain in tool_input.get("allowed_domains") or []:
                    if isinstance(domain, str) and domain not in known:
                        known.add(domain)
                        result.requested_domains.append((domain, str(tool_input.get("description") or "")))
        return activity
    if etype == "result":
        result.session_id = event.get("session_id") or result.session_id
        result.text = event.get("result") or ""
        result.is_error = bool(event.get("is_error"))
        result.cost_usd = event.get("total_cost_usd")
        result.duration_ms = event.get("duration_ms")
        result.errors = [str(e) for e in event.get("errors") or []]
        if result.is_error:
            result.limit_reset_at = parse_limit(" ".join([result.text, *result.errors]))
    return None


def parse_limit(text: str, now: float | None = None) -> float | None:
    """契約の上限に当たったか。当たっていれば明ける時刻（エポック秒）、分からなければ UNKNOWN_LIMIT_RESET。"""
    if not _USAGE_LIMIT.search(text):
        return None
    epoch = _LIMIT_EPOCH.search(text)
    if epoch:
        return float(epoch.group(1)[:10])
    when = _LIMIT_RESETS.search(text)
    if not when:
        return UNKNOWN_LIMIT_RESET
    hour, minute, ampm = int(when.group(1)), int(when.group(2) or 0), (when.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return UNKNOWN_LIMIT_RESET
    base = datetime.fromtimestamp(time.time() if now is None else now)
    reset = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # 「3pm に明ける」が今より前なら、明日の 3pm のこと
    return (reset if reset > base else reset + timedelta(days=1)).timestamp()


def _kill_group(pid: int) -> None:
    """claude とその中で動いている Bash を、プロセスグループごと止める。

    start_new_session=True で起動しているので、グループIDは claude の pid と同じ。
    claude 本体を回収したあとでも、残った子プロセスを止められるよう getpgid は使わない。
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


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
        await asyncio.wait_for(read_stdout(), timeout=run_timeout_seconds(config, ws))
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
