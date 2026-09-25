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
