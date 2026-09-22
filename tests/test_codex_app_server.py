import pytest

from kei_agent.agent_policy import policy_for
from kei_agent.codex_app_server import AppServerUnavailable, build_command, build_turn_input, resolve_apps


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
