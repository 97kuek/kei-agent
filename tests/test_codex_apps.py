"""Codex App の ID を表示名から引く（モデルは動かさない）。"""

import os

import pytest

from kei_agent import codex_apps
from kei_agent.codex_apps import AppsUnavailable, app_ids, installed_apps, resolve_apps


def test_resolve_apps_rejects_an_app_that_cannot_be_called():
    installed = [{"id": "box-id", "runtimeName": "Box", "enabled": True, "callable": False}]
    with pytest.raises(AppsUnavailable, match="Box"):
        resolve_apps(installed, ["Box"])


def test_resolve_apps_maps_names_to_current_ids():
    installed = [
        {"id": "calendar-id", "runtimeName": "Microsoft Outlook Calendar", "enabled": True, "callable": True},
        {"id": "mail-id", "runtimeName": "Microsoft Outlook Email", "enabled": True, "callable": True},
        {"id": "gmail-id", "runtimeName": "Gmail", "enabled": True, "callable": True},
    ]
    assert resolve_apps(installed, ["Microsoft Outlook Email", "Microsoft Outlook Calendar"]) == {
        "Microsoft Outlook Calendar": "calendar-id", "Microsoft Outlook Email": "mail-id"}


def _server(tmp_path, body: str):
    server = tmp_path / "fake-app-server.py"
    server.write_text("#!/usr/bin/env python3\nimport json, os, sys, time\n"
                      f"open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid()))\n" + body)
    server.chmod(0o755)
    return server


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_installed_apps_reads_a_large_json_rpc_line(tmp_path):
    """Box などが返す64KiB超の1行でも読める。問い合わせには知らないメソッドとして答える。"""
    server = _server(tmp_path, """
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "app/installed":
        print(json.dumps({"method": "item/commandExecution/requestApproval", "id": 99}), flush=True)
        apps = [{"id": "box-id", "runtimeName": "Box", "enabled": True, "callable": True}]
        print(json.dumps({"id": request["id"], "result": {"apps": apps, "padding": "x" * 100_000}}), flush=True)
    elif "id" in request and "method" in request:
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
""")
    apps = await installed_apps(str(server), timeout_seconds=5)
    assert [app["id"] for app in apps] == ["box-id"]
    assert not _alive(int((tmp_path / "pid").read_text()))


async def test_silent_app_server_is_stopped_on_timeout(tmp_path):
    server = tmp_path / "silent-app-server.sh"
    server.write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'pid'}\nexec sleep 60\n")
    server.chmod(0o755)
    with pytest.raises(TimeoutError):
        await installed_apps(str(server), timeout_seconds=1)
    assert not _alive(int((tmp_path / "pid").read_text()))


async def test_app_ids_reuses_the_list_for_a_while(monkeypatch):
    calls = []

    async def installed(codex_bin, timeout_seconds=30):
        calls.append(codex_bin)
        return [{"id": "box-id", "runtimeName": "Box", "enabled": True, "callable": True}]

    monkeypatch.setattr(codex_apps, "installed_apps", installed)
    monkeypatch.setattr(codex_apps, "_cache", {})
    assert await app_ids("codex", ["Box"]) == {"Box": "box-id"}
    assert await app_ids("codex", ["Box"]) == {"Box": "box-id"}
    assert await app_ids("codex", []) == {}
    assert calls == ["codex"]


async def test_app_ids_says_when_the_list_cannot_be_read(monkeypatch):
    async def broken(codex_bin, timeout_seconds=30):
        raise OSError("codex がない")

    monkeypatch.setattr(codex_apps, "installed_apps", broken)
    monkeypatch.setattr(codex_apps, "_cache", {})
    with pytest.raises(AppsUnavailable, match="一覧"):
        await app_ids("codex", ["Box"])
