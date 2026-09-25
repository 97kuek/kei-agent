"""Codex App Server を agent ごとの一時 App 設定で実行する。"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from kei_agent import guard
from kei_agent.agent_policy import AppPolicy
from kei_agent.provider_permissions import PROFILE_NAME, CapabilityUnavailable, PermissionProfile
from kei_agent.runner import RunResult, codex_instructions_setting, parse_limit, verify_codex_profile

# App connector の検索結果は、Box のプレビューなどで標準の64KiBを超えることがある。
# JSON-RPC 1行を十分に読める上限。これを超えた場合はメモリを使い続けず、利用者向けのエラーにする。
APP_SERVER_LINE_LIMIT = 8 * 1024 * 1024
# App Server から来る問い合わせ（承認など）への断り。人に聞けないので、すべて断る
DECLINES: dict[str, dict[str, str]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "mcpServer/elicitation/request": {"action": "decline"},
}
# 断り方が決まっていない問い合わせは、JSON-RPC の「知らないメソッド」で返す
METHOD_NOT_FOUND = -32601


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


_CONFIG_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _checked_key(value: str) -> str:
    if not _CONFIG_KEY.fullmatch(value):
        raise AppServerUnavailable("接続の設定名を安全に指定できません")
    return value


def build_command(codex_bin: str, app_ids: Sequence[str] = (), read_only: bool = False,
                  instructions: str = "", profile: PermissionProfile | None = None,
                  disabled_mcp_names: Sequence[str] = (),
                  disabled_app_ids: Sequence[str] = (),
                  read_only_app_ids: Sequence[str] = ()) -> list[str]:
    command = [codex_bin, "app-server", "--stdio"]
    if profile is not None:
        command.append("--strict-config")
        for name in disabled_mcp_names:
            command += ["-c", f"mcp_servers.{_checked_key(name)}.enabled=false"]
        for setting in profile.config_overrides:
            command += ["-c", setting]
    if instructions:
        command += ["-c", codex_instructions_setting(instructions)]
    if app_ids:
        command += ["-c", "apps._default.enabled=false"]
        for app_id in disabled_app_ids:
            command += ["-c", f"apps.{_checked_key(app_id)}.enabled=false"]
        for app_id in app_ids:
            command += ["-c", f"apps.{_checked_key(app_id)}.enabled=true"]
            if read_only or app_id in read_only_app_ids:
                command += ["-c", f"apps.{app_id}.destructive_enabled=false"]
    return command


def build_turn_input(prompt: str, app_ids: Sequence[str]) -> list[dict[str, str]]:
    return [{"type": "text", "text": prompt}, *[
        {"type": "mention", "name": app_id, "path": f"app://{app_id}"} for app_id in app_ids
    ]]


def build_thread_params(model: str, cwd: Path | None = None,
                        permissions: str | None = None) -> dict[str, str]:
    params = {"model": model} if model else {}
    if cwd is not None:
        params["cwd"] = str(cwd)
    if permissions is not None:
        params["permissions"] = permissions
    return params


def apply_app_event(result: RunResult, event: dict[str, Any], turn_id: str) -> bool:
    """App の途中メッセージを保留し、正常な turn 完了時だけ確定する。"""
    payload = event.get("params") or event
    item = payload.get("item") or {}
    if event.get("method") == "item/completed" and item.get("type") in {"agent_message", "agentMessage"}:
        result._final_candidate = str(item.get("text") or result._final_candidate)
    if event.get("method") == "turn/failed" and (payload.get("turn") or {}).get("id") == turn_id:
        error = (payload.get("turn") or {}).get("error") or payload.get("error") or "Codex App Server の実行に失敗しました"
        message = str(error.get("message") if isinstance(error, dict) else error)
        result.errors.append(message)
        result.is_error = True
        result.limit_reset_at = parse_limit(message)
        result.failure_kind = "quota" if result.limit_reset_at is not None else "runtime"
        return True
    if event.get("method") == "turn/completed" and (payload.get("turn") or {}).get("id") == turn_id:
        if (payload.get("turn") or {}).get("status") in {None, "completed"}:
            result.text = result._final_candidate
            if not result.text.strip():
                result.is_error = True
                result.failure_kind = "runtime"
        else:
            result.is_error = True
            result.failure_kind = "runtime"
        return True
    return False


def server_request_reply(event: dict[str, Any]) -> dict[str, Any]:
    """App Server からの問い合わせに返す答え。承認は断り、それ以外は知らないと返す。"""
    method = str(event.get("method") or "")
    if method in DECLINES:
        return {"id": event["id"], "result": DECLINES[method]}
    return {"id": event["id"], "error": {"code": METHOD_NOT_FOUND, "message": f"unsupported: {method}"}}


def _is_server_request(event: dict[str, Any]) -> bool:
    return "method" in event and "id" in event


async def discover_enabled_mcp_names_strict(codex_bin: str = "codex") -> frozenset[str]:
    """App 実行前にユーザー設定の MCP を列挙する。読めなければ安全側で停止。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "mcp", "list", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        entries = json.loads(stdout)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Codex MCP の設定を確認できません") from exc
    if proc.returncode or not isinstance(entries, list):
        raise RuntimeError("Codex MCP の設定を確認できません")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            raise RuntimeError("Codex MCP の設定を確認できません")
        if entry["enabled"]:
            name = entry.get("name")
            if not isinstance(name, str):
                raise RuntimeError("Codex MCP の設定を確認できません")
            names.add(name)
    return frozenset(names)


class AppServerClient:
    """一時プロセスだけを使う App Server JSON-RPC client。"""

    def __init__(self, codex_bin: str = "codex", timeout_seconds: float = 300):
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self._ids = itertools.count(1)

    async def _verify_profile(self, cwd: Path, profile: PermissionProfile) -> None:
        """App Server 起動前に同じ権限 profile が OS で実行可能か確かめる。"""
        try:
            await verify_codex_profile(self.codex_bin, cwd, profile)
        except CapabilityUnavailable as exc:
            raise AppServerUnavailable(str(exc)) from exc

    async def _start(self, app_ids: Sequence[str] = (), read_only: bool = False,
                     instructions: str = "", profile: PermissionProfile | None = None,
                     disabled_mcp_names: Sequence[str] = (),
                     disabled_app_ids: Sequence[str] = (),
                     read_only_app_ids: Sequence[str] = ()):
        proc = await asyncio.create_subprocess_exec(
            *build_command(self.codex_bin, app_ids, read_only, instructions, profile,
                           disabled_mcp_names, disabled_app_ids, read_only_app_ids),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Slack などの鍵を App Server とその子（connector や Bash）に渡さない
            env=guard.strip_env(dict(os.environ)),
            limit=APP_SERVER_LINE_LIMIT,
        )
        try:
            await self._request(proc, "initialize", {
                "clientInfo": {"name": "kei_agent", "title": "Kei Agent", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await self._notify(proc, "initialized", {})
        except BaseException:
            # 呼び出し元には proc が渡らないので、ここで止めないと残り続ける
            await self._stop(proc)
            raise
        return proc

    async def _send(self, proc, message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def _notify(self, proc, method: str, params: dict[str, Any]) -> None:
        await self._send(proc, {"method": method, "params": params})

    async def _next_event(self, proc) -> dict[str, Any] | None:
        """次の通知か返事。問い合わせにはその場で断りを返す。終わりなら None。"""
        while raw := await self._readline(proc):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if _is_server_request(event):
                await self._send(proc, server_request_reply(event))
                continue
            return event
        return None

    async def _request(self, proc, method: str, params: dict[str, Any], pending: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        request_id = next(self._ids)
        await self._send(proc, {"method": method, "id": request_id, "params": params})
        while (event := await self._next_event(proc)) is not None:
            if event.get("id") != request_id:
                if pending is not None:
                    pending.append(event)
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
                  instructions: str = "", cwd: Path | None = None,
                  profile: PermissionProfile | None = None) -> RunResult:
        result = RunResult()
        proc = None
        try:
            # 1行ごとの上限とは別に、全体の上限時間で止める（途中経過が流れ続けても終わるように）
            async with asyncio.timeout(self.timeout_seconds):
                if profile is None or cwd is None:
                    raise AppServerUnavailable("Codex の権限 profile と作業場が未設定です")
                await self._verify_profile(cwd, profile)
                installed = await self.installed()
                app_ids = resolve_apps(installed, policy)
                read_only_app_ids = tuple(str(item["id"]) for item in installed
                                          if item.get("runtimeName") in policy.read_only_app_names and item.get("id"))
                disabled_app_ids = tuple(str(item["id"]) for item in installed
                                         if item.get("id") and item["id"] not in app_ids)
                mcp_names = await discover_enabled_mcp_names_strict(self.codex_bin)
                proc = await self._start(app_ids, policy.read_only, instructions, profile,
                                         sorted(mcp_names), disabled_app_ids, read_only_app_ids)
                # 設定をかけた二つ目の process でも readiness を確認する。
                active_apps = (await self._request(proc, "app/installed", {"forceRefresh": True})).get("apps") or []
                resolve_apps(active_apps, policy)
                if any(item.get("enabled") and item.get("callable") and item.get("id") not in app_ids
                       for item in active_apps):
                    raise AppServerUnavailable("許可外の Codex App が有効です")
                thread_params: dict[str, Any] = build_thread_params(model, cwd, PROFILE_NAME)
                pending: list[dict[str, Any]] = []
                thread = await self._request(proc, "thread/start", thread_params, pending)
                thread_id = str((thread.get("thread") or {}).get("id") or "")
                if not thread_id:
                    raise AppServerUnavailable("Codex thread を開始できません")
                result.session_id = thread_id
                turn = await self._request(proc, "turn/start", {
                    "threadId": thread_id, "input": build_turn_input(prompt, app_ids),
                    "reasoningEffort": reasoning_effort}, pending)
                turn_id = str((turn.get("turn") or {}).get("id") or "")
                while True:
                    event = pending.pop(0) if pending else await self._next_event(proc)
                    if event is None:
                        break
                    if apply_app_event(result, event, turn_id):
                        return result
                result.is_error = True
                result.errors.append("Codex App Server の実行が終了しました")
                return result
        except TimeoutError:
            result.is_error = True
            result.timed_out = True
            result.failure_kind = "timeout"
            result.text = ""
            result.errors.append(f"Codex App Server が {self.timeout_seconds:.0f} 秒で終わりませんでした")
            return result
        except (AppServerUnavailable, RuntimeError) as exc:
            result.is_error = True
            result.errors.append(str(exc))
            return result
        finally:
            if proc is not None:
                await self._stop(proc)

    async def _readline(self, proc) -> bytes:
        """JSON-RPCの1行を読み、上限超過を生のasyncio例外のまま出さない。"""
        assert proc.stdout is not None
        try:
            return await asyncio.wait_for(proc.stdout.readline(), self.timeout_seconds)
        except ValueError as exc:
            if "chunk exceed the limit" in str(exc) or "chunk is longer than limit" in str(exc):
                raise AppServerUnavailable("Codex の連携結果が大きすぎて読み取れません") from None
            raise

    async def _stop(self, proc) -> None:
        if proc.returncode is None:
            proc.terminate()
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
        if proc.returncode is None:
            proc.kill()
