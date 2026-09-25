import asyncio

import pytest

from kei_agent.agent_policy import policy_for
from kei_agent.codex_app_server import (
    AppServerClient,
    AppServerUnavailable,
    apply_app_event,
    build_command,
    build_thread_params,
    build_turn_input,
    resolve_apps,
)
from kei_agent.provider_permissions import PROFILE_NAME, connector_profile
from kei_agent.runner import RunResult


def test_resolve_apps_rejects_non_callable_box():
    installed = [{"id": "box-id", "runtimeName": "Box", "enabled": True, "callable": False}]
    with pytest.raises(AppServerUnavailable, match="Box"):
        resolve_apps(installed, policy_for("course"))


def test_resolve_apps_uses_current_ids_without_persisting_them():
    installed = [
        {"id": "calendar-id", "runtimeName": "Microsoft Outlook Calendar", "enabled": True, "callable": True},
        {"id": "mail-id", "runtimeName": "Microsoft Outlook Email", "enabled": True, "callable": True},
    ]
    assert resolve_apps(installed, policy_for("work")) == ("calendar-id", "mail-id")
    assert build_turn_input("予定を教えて", ("mail-id",)) == [
        {"type": "text", "text": "予定を教えて"},
        {"type": "mention", "name": "mail-id", "path": "app://mail-id"},
    ]


def test_command_disables_every_app_except_resolved_ids():
    command = build_command("codex-test", ("box-id", "notion-id"))
    assert command[:3] == ["codex-test", "app-server", "--stdio"]
    assert "apps._default.enabled=false" in command
    assert "apps.box-id.enabled=true" in command
    assert "apps.notion-id.enabled=true" in command


def test_course_can_write_notion_but_disables_box_destructive_operations():
    command = build_command("codex-test", ("box-id", "notion-id"), read_only_app_ids=("box-id",))

    assert "apps.box-id.destructive_enabled=false" in command
    assert "apps.notion-id.destructive_enabled=false" not in command


def test_app_server_command_receives_agent_developer_instructions():
    command = build_command("codex-test", ("box-id",), instructions="僕は Kei Agent の大学担当")

    assert "developer_instructions=\"僕は Kei Agent の大学担当\"" in command


def test_app_thread_starts_in_its_agent_scoped_workspace(tmp_path):
    assert build_thread_params("gpt-6-luna", tmp_path / "course") == {
        "model": "gpt-6-luna", "cwd": str(tmp_path / "course")
    }


def test_app_thread_uses_named_permission_profile(tmp_path):
    assert build_thread_params("gpt-6-luna", tmp_path / "course", PROFILE_NAME)["permissions"] == PROFILE_NAME


def test_app_server_process_loads_scoped_profile(tmp_path):
    profile = connector_profile(tmp_path / "course")
    command = build_command("codex-test", ("box-id",), profile=profile,
                            disabled_mcp_names=("user-server",))

    assert "--strict-config" in command
    assert all(setting in command for setting in profile.config_overrides)
    assert "mcp_servers.user-server.enabled=false" in command


def test_scoped_app_command_disables_unapproved_explicit_apps(tmp_path):
    command = build_command("codex-test", ("box-id",), profile=connector_profile(tmp_path),
                            disabled_app_ids=("other-id",))
    assert "apps.other-id.enabled=false" in command


def test_app_events_hold_messages_until_successful_turn_completion():
    result = RunResult()
    assert not apply_app_event(result, {"method": "item/agentMessage/delta", "params": {
        "delta": "内部の途中経過"}}, "turn-1")
    assert result.text == ""
    assert not apply_app_event(result, {"method": "item/completed", "params": {
        "item": {"type": "agentMessage", "text": "<<kei-agent-final>>答え<<kei-agent-final-end>>"}}}, "turn-1")
    assert result.text == ""
    assert apply_app_event(result, {"method": "turn/completed", "params": {
        "turn": {"id": "turn-1", "status": "completed"}}}, "turn-1")
    assert result.text == "<<kei-agent-final>>答え<<kei-agent-final-end>>"


def test_app_completion_without_final_message_is_a_failure():
    result = RunResult()
    assert apply_app_event(result, {"method": "turn/completed", "params": {
        "turn": {"id": "turn-1", "status": "completed"}}}, "turn-1")
    assert result.is_error and not result.text


def test_app_quota_failure_is_preserved_for_provider_specific_deferral():
    result = RunResult()

    assert apply_app_event(result, {"method": "turn/failed", "params": {
        "turn": {"id": "turn-1", "error": {"message": "usage limit reached"}}}}, "turn-1")

    assert result.is_error
    assert result.limit_reset_at is not None
    assert result.failure_kind == "quota"


async def test_app_server_refuses_to_start_when_local_profile_cannot_execute(tmp_path, monkeypatch):
    client = AppServerClient("codex-test")

    async def unavailable(*_args, **_kwargs):
        raise AppServerUnavailable("権限を強制できません")

    async def should_not_list_apps():
        raise AssertionError("app discovery must not run")

    monkeypatch.setattr(client, "_verify_profile", unavailable)
    monkeypatch.setattr(client, "installed", should_not_list_apps)
    result = await client.run("質問", policy_for("course"), "gpt-6-luna", "medium",
                              cwd=tmp_path, profile=connector_profile(tmp_path))
    assert result.is_error
    assert "権限を強制できません" in result.errors[0]


async def test_client_accepts_a_large_json_rpc_response(tmp_path):
    """Box などが返す64KiB超の1行JSONでも、App Server clientが落ちない。"""
    server = tmp_path / "fake-app-server.py"
    server.write_text("""#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" in request:
        print(json.dumps({"id": request["id"], "result": {"apps": [], "padding": "x" * 100_000}}), flush=True)
""")
    server.chmod(0o755)

    assert await AppServerClient(str(server), timeout_seconds=3).installed() == []


async def test_strict_mcp_discovery_rejects_unreadable_config(monkeypatch):
    from kei_agent import codex_app_server

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"private error"

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(codex_app_server.asyncio, "create_subprocess_exec", create)
    with pytest.raises(RuntimeError, match="MCP の設定"):
        await codex_app_server.discover_enabled_mcp_names_strict("codex")


def _fake_server(tmp_path, body: str):
    server = tmp_path / "fake-app-server.py"
    server.write_text("#!/usr/bin/env python3\nimport json, os, sys, time\n"
                      f"open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid()))\n" + body)
    server.chmod(0o755)
    return server


def _alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_initialize_timeout_stops_the_spawned_process(tmp_path):
    """initialize に答えない App Server を、起動したまま残さない。"""
    server = tmp_path / "silent-app-server.sh"
    server.write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'pid'}\nexec sleep 60\n")
    server.chmod(0o755)

    with pytest.raises(TimeoutError):
        await AppServerClient(str(server), timeout_seconds=2).installed()

    pid = int((tmp_path / "pid").read_text())
    assert not _alive(pid)


async def _prepared_client(server, monkeypatch, timeout_seconds: float = 5):
    from kei_agent import codex_app_server

    client = AppServerClient(str(server), timeout_seconds=timeout_seconds)

    async def verified(*_args, **_kwargs):
        return None

    async def installed():
        return []

    async def no_mcp(_codex_bin):
        return frozenset()

    monkeypatch.setattr(client, "_verify_profile", verified)
    monkeypatch.setattr(client, "installed", installed)
    monkeypatch.setattr(codex_app_server, "discover_enabled_mcp_names_strict", no_mcp)
    return client


# initialize / app/installed / thread/start / turn/start に答える、最小の App Server
_RESPOND = """
def send(obj):
    print(json.dumps(obj), flush=True)

def answer(request):
    method = request.get("method")
    if method == "thread/start":
        send({"id": request["id"], "result": {"thread": {"id": "thread-1"}}})
    elif method == "turn/start":
        send({"id": request["id"], "result": {"turn": {"id": "turn-1"}}})
        return True
    elif method == "app/installed":
        send({"id": request["id"], "result": {"apps": []}})
    elif "id" in request:
        send({"id": request["id"], "result": {}})
    return False
"""


async def test_server_approval_request_is_declined_instead_of_hanging(tmp_path, monkeypatch):
    """App Server からの承認の問い合わせには断りを返し、turn を進める。"""
    server = _fake_server(tmp_path, _RESPOND + f"""
for line in sys.stdin:
    request = json.loads(line)
    if answer(request):
        send({{"id": 1, "method": "item/commandExecution/requestApproval", "params": {{"command": "rm -rf /"}}}})
        reply = json.loads(sys.stdin.readline())
        open({str(tmp_path / 'reply.json')!r}, "w").write(json.dumps(reply))
        send({{"method": "item/completed", "params": {{"item": {{"type": "agentMessage", "text": "できた"}}}}}})
        send({{"method": "turn/completed", "params": {{"turn": {{"id": "turn-1", "status": "completed"}}}}}})
""")
    client = await _prepared_client(server, monkeypatch)

    result = await client.run("質問", policy_for("research"), "gpt-6-luna", "medium",
                              cwd=tmp_path, profile=connector_profile(tmp_path))

    assert not result.is_error and result.text == "できた"
    import json
    assert json.loads((tmp_path / "reply.json").read_text()) == {"id": 1, "result": {"decision": "decline"}}


async def test_unknown_server_request_gets_a_json_rpc_error(tmp_path, monkeypatch):
    server = _fake_server(tmp_path, _RESPOND + f"""
for line in sys.stdin:
    request = json.loads(line)
    if answer(request):
        send({{"id": "x", "method": "item/tool/requestUserInput", "params": {{}}}})
        reply = json.loads(sys.stdin.readline())
        open({str(tmp_path / 'reply.json')!r}, "w").write(json.dumps(reply))
        send({{"method": "turn/completed", "params": {{"turn": {{"id": "turn-1", "status": "failed"}}}}}})
""")
    client = await _prepared_client(server, monkeypatch)

    result = await client.run("質問", policy_for("research"), "gpt-6-luna", "medium",
                              cwd=tmp_path, profile=connector_profile(tmp_path))

    import json
    reply = json.loads((tmp_path / "reply.json").read_text())
    assert reply["id"] == "x" and reply["error"]["code"] == -32601
    assert result.is_error


async def test_run_has_an_overall_deadline_even_if_events_keep_coming(tmp_path, monkeypatch):
    """1行ずつは届き続けても、全体の上限時間で止める。"""
    server = _fake_server(tmp_path, _RESPOND + """
for line in sys.stdin:
    request = json.loads(line)
    if answer(request):
        while True:
            send({"method": "item/agentMessage/delta", "params": {"delta": "…"}})
            time.sleep(0.1)
""")
    client = await _prepared_client(server, monkeypatch, timeout_seconds=1)

    result = await asyncio.wait_for(
        client.run("質問", policy_for("research"), "gpt-6-luna", "medium",
                   cwd=tmp_path, profile=connector_profile(tmp_path)), timeout=10)

    assert result.is_error and result.timed_out and result.failure_kind == "timeout"
    assert not _alive(int((tmp_path / "pid").read_text()))
