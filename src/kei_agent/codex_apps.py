"""Codex App（アカウントの連携）の ID を、表示名から引く。

ID はアカウントごとに違い、入れ直すと変わるので保存しない。実行の前に `codex app-server` に
一覧だけを聞く（モデルは動かさない）。Codex を動かすのは runner の `codex exec` だけ。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from kei_agent import guard

# Box のプレビューなどで、JSON-RPC の1行が標準の64KiBを超えることがある
APP_SERVER_LINE_LIMIT = 8 * 1024 * 1024
# 一覧を使い回す秒数。連携を足し外ししても、この時間がたてば反映される
CACHE_SECONDS = 600
# App Server からの問い合わせ（承認など）には答えられないので、知らないメソッドとして返す
METHOD_NOT_FOUND = -32601

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class AppsUnavailable(RuntimeError):
    """必要な連携が、いまの Codex のログインでは使えない。"""


def resolve_apps(installed: Iterable[dict[str, Any]], names: Iterable[str]) -> dict[str, str]:
    """表示名 → いまの ID。1つでも使えない状態なら、その名前を言って止める。"""
    by_name = {str(item.get("runtimeName") or ""): item for item in installed}
    ids: dict[str, str] = {}
    for name in sorted(set(names)):
        item = by_name.get(name)
        if not item or not item.get("enabled") or not item.get("callable"):
            raise AppsUnavailable(f"{name} が Codex で使える状態ではありません（Codex の連携を確認してください）")
        app_id = str(item.get("id") or "")
        if not app_id:
            raise AppsUnavailable(f"{name} の接続情報を確認できません")
        ids[name] = app_id
    return ids


async def installed_apps(codex_bin: str = "codex", timeout_seconds: float = 30) -> list[dict[str, Any]]:
    """いまの Codex のログインで入っている App の一覧。"""
    proc = await asyncio.create_subprocess_exec(
        codex_bin, "app-server", "--stdio",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        # Slack などの鍵を App Server に渡さない
        env=guard.strip_env(dict(os.environ)),
        limit=APP_SERVER_LINE_LIMIT,
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            await _request(proc, 1, "initialize", {
                "clientInfo": {"name": "kei_agent", "title": "Kei Agent", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await _send(proc, {"method": "initialized", "params": {}})
            result = await _request(proc, 2, "app/installed", {"forceRefresh": True})
    finally:
        await _stop(proc)
    return [item for item in result.get("apps") or [] if isinstance(item, dict)]


async def app_ids(codex_bin: str, names: Iterable[str]) -> dict[str, str]:
    """表示名 → ID。一覧は CACHE_SECONDS のあいだ使い回す。"""
    names = tuple(names)
    if not names:
        return {}
    cached = _cache.get(codex_bin)
    if cached is None or time.monotonic() - cached[0] > CACHE_SECONDS:
        try:
            cached = (time.monotonic(), await installed_apps(codex_bin))
        except (OSError, TimeoutError, ValueError) as e:
            raise AppsUnavailable("Codex の連携の一覧を取得できません") from e
        _cache[codex_bin] = cached
    return resolve_apps(cached[1], names)


async def _send(proc, message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()


async def _request(proc, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    await _send(proc, {"method": method, "id": request_id, "params": params})
    assert proc.stdout is not None
    while raw := await proc.stdout.readline():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if "method" in event and "id" in event:
            await _send(proc, {"id": event["id"], "error": {"code": METHOD_NOT_FOUND,
                                                            "message": f"unsupported: {event['method']}"}})
            continue
        if event.get("id") != request_id:
            continue
        if event.get("error"):
            raise AppsUnavailable(f"Codex App Server の {method} に失敗しました")
        return event.get("result") or {}
    raise AppsUnavailable(f"Codex App Server の {method} が応答しません")


async def _stop(proc) -> None:
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.terminate()
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 3)
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 3)
