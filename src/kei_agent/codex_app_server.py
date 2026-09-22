"""Codex App Server を agent ごとの一時 App 設定で実行する。"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any

from kei_agent.agent_policy import AppPolicy
from kei_agent.runner import RunResult


class AppServerUnavailable(RuntimeError):
    """必要な接続が現在の Codex App Server から使えない。"""


def resolve_apps(installed: Sequence[dict[str, Any]], policy: AppPolicy) -> tuple[str, ...]:
    """表示名から今回だけ有効な App ID を解決する。ID は保存しない。"""
    by_name = {str(item.get("runtimeName") or ""): item for item in installed}
    ids: list[str] = []
    for name in sorted(policy.app_names):
        item = by_name.get(name)
        if not item or not item.get("enabled") or not item.get("callable"):
            raise AppServerUnavailable(f"{name} が Codex で使える状態ではありません")
        app_id = str(item.get("id") or "")
        if not app_id:
            raise AppServerUnavailable(f"{name} の接続情報を確認できません")
        ids.append(app_id)
    return tuple(ids)


def build_command(codex_bin: str, app_ids: Sequence[str] = (), read_only: bool = False) -> list[str]:
    command = [codex_bin, "app-server", "--stdio"]
    if app_ids:
        command += ["-c", "apps._default.enabled=false"]
        for app_id in app_ids:
            command += ["-c", f"apps.{app_id}.enabled=true"]
            if read_only:
                command += ["-c", f"apps.{app_id}.destructive_enabled=false"]
    return command


def build_turn_input(prompt: str, app_ids: Sequence[str]) -> list[dict[str, str]]:
    return [{"type": "text", "text": prompt}, *[
        {"type": "mention", "name": app_id, "path": f"app://{app_id}"} for app_id in app_ids
    ]]


class AppServerClient:
    """一時プロセスだけを使う App Server JSON-RPC client。"""

    def __init__(self, codex_bin: str = "codex", timeout_seconds: float = 300):
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self._ids = itertools.count(1)

    async def _start(self, app_ids: Sequence[str] = (), read_only: bool = False):
        proc = await asyncio.create_subprocess_exec(
            *build_command(self.codex_bin, app_ids, read_only),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._request(proc, "initialize", {
            "clientInfo": {"name": "kei_agent", "title": "Kei Agent", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await self._notify(proc, "initialized", {})
        return proc

    async def _notify(self, proc, method: str, params: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((__import__("json").dumps({"method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()

    async def _request(self, proc, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = next(self._ids)
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((__import__("json").dumps({"method": method, "id": request_id, "params": params}) + "\n").encode())
        await proc.stdin.drain()
        while raw := await asyncio.wait_for(proc.stdout.readline(), self.timeout_seconds):
            try:
                event = __import__("json").loads(raw)
            except ValueError:
                continue
            if event.get("id") != request_id:
                continue
            if event.get("error"):
                raise AppServerUnavailable(f"Codex App Server の {method} に失敗しました")
            return event.get("result") or {}
        raise AppServerUnavailable(f"Codex App Server の {method} が応答しません")

    async def installed(self) -> list[dict[str, Any]]:
        proc = await self._start()
        try:
            return list((await self._request(proc, "app/installed", {"forceRefresh": True})).get("apps") or [])
        finally:
            await self._stop(proc)

    async def run(self, prompt: str, policy: AppPolicy, model: str, reasoning_effort: str,
                  on_activity: Callable[[str], Awaitable[None]] | None = None,
                  on_text: Callable[[str], Awaitable[None]] | None = None) -> RunResult:
        app_ids = resolve_apps(await self.installed(), policy)
        proc = await self._start(app_ids, policy.read_only)
        result = RunResult()
        try:
            # 設定をかけた二つ目の process でも readiness を確認する。
            resolve_apps((await self._request(proc, "app/installed", {"forceRefresh": True})).get("apps") or [], policy)
            thread_params: dict[str, Any] = {"model": model} if model else {}
            thread = await self._request(proc, "thread/start", thread_params)
            thread_id = str((thread.get("thread") or {}).get("id") or "")
            if not thread_id:
                raise AppServerUnavailable("Codex thread を開始できません")
            result.session_id = thread_id
            turn = await self._request(proc, "turn/start", {"threadId": thread_id, "input": build_turn_input(prompt, app_ids),
                                                               "reasoningEffort": reasoning_effort})
            turn_id = str((turn.get("turn") or {}).get("id") or "")
            assert proc.stdout is not None
            while raw := await asyncio.wait_for(proc.stdout.readline(), self.timeout_seconds):
                try:
                    event = __import__("json").loads(raw)
                except ValueError:
                    continue
                item = event.get("item") or {}
                if event.get("method") == "item/completed" and item.get("type") == "agent_message":
                    result.text = str(item.get("text") or result.text)
                    if on_text and result.text:
                        await on_text(result.text)
                if event.get("method") == "turn/completed" and (event.get("params") or {}).get("turn", {}).get("id") == turn_id:
                    return result
            result.is_error = True
            result.errors.append("Codex App Server の実行が終了しました")
            return result
        except (AppServerUnavailable, TimeoutError) as exc:
            result.is_error = True
            result.errors.append(str(exc))
            return result
        finally:
            await self._stop(proc)

    async def _stop(self, proc) -> None:
        if proc.returncode is None:
            proc.terminate()
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
        if proc.returncode is None:
            proc.kill()
