"""Codex CLI の read-only 疎通確認と、MCP 名だけの検出。"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROBE_ACKNOWLEDGEMENT = "KEI_AGENT_CODEX_OK"
PROBE_PROMPT = (
    "Reply with exactly KEI_AGENT_CODEX_OK. Do not read files, use tools, or explain."
)


@dataclass(frozen=True)
class ProbeResult:
    available: bool
    thread_id: str | None = None
    error: str | None = None


def build_probe_command(codex_bin: str, cwd: Path) -> list[str]:
    """既存の session や作業場を使わない、最小の Codex CLI 呼び出し。"""
    return [
        codex_bin,
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--cd",
        str(cwd),
        "--skip-git-repo-check",
        "--ephemeral",
        "-",
    ]


def parse_probe_events(lines: Iterable[str]) -> ProbeResult:
    """JSONL からスレッドIDと固定応答だけを取り出す。"""
    thread_id: str | None = None
    acknowledged = False
    failure: str | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and item.get("text") == PROBE_ACKNOWLEDGEMENT:
                acknowledged = True
        if event.get("type") in {"error", "turn.failed"}:
            failure = "Codex の固定プローブが失敗しました"
    if failure:
        return ProbeResult(available=False, thread_id=thread_id, error=failure)
    if acknowledged:
        return ProbeResult(available=True, thread_id=thread_id)
    return ProbeResult(
        available=False,
        thread_id=thread_id,
        error=failure or "Codex の固定プローブ応答を確認できませんでした",
    )


def parse_mcp_names(payload: str | bytes) -> frozenset[str]:
    """`codex mcp list --json` から有効なサーバー名だけを取り出す。"""
    try:
        entries = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return frozenset()
    if not isinstance(entries, list):
        return frozenset()
    return frozenset(
        entry["name"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("enabled") is True and isinstance(entry.get("name"), str)
    )


async def run_probe(codex_bin: str = "codex") -> ProbeResult:
    """一時ディレクトリで Codex のログイン済み read-only 疎通だけを確認する。"""
    with tempfile.TemporaryDirectory(prefix="kei-agent-codex-probe-") as directory:
        try:
            proc = await asyncio.create_subprocess_exec(
                *build_probe_command(codex_bin, Path(directory)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return ProbeResult(available=False, error="Codex CLI を起動できませんでした")
        stdout, _ = await proc.communicate(PROBE_PROMPT.encode("utf-8"))
    result = parse_probe_events(stdout.decode("utf-8", "replace").splitlines())
    if proc.returncode and result.error is None:
        return ProbeResult(
            available=False,
            thread_id=result.thread_id,
            error="Codex の固定プローブが失敗しました",
        )
    return result


async def discover_mcp_names(codex_bin: str = "codex") -> frozenset[str]:
    """設定済みかつ有効な MCP 名を読む。接続内容やエラー本文は公開しない。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin,
            "mcp",
            "list",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return frozenset()
    stdout, _ = await proc.communicate()
    if proc.returncode:
        return frozenset()
    return parse_mcp_names(stdout)
