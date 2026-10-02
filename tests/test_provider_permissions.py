
import pytest

from kei_agent.execution import runner
from kei_agent.execution.execution_contract import resolve_contract
from kei_agent.execution.guard import DEFAULT_DENY_READ, denied_reads
from kei_agent.execution.model_policy import UseCase, resolve
from kei_agent.execution.provider_permissions import CapabilityUnavailable, preflight
from kei_agent.workspaces import themes


def _contract(config, *, provider="codex", read_only=False):
    request = runner.ExecutionRequest(
        themes.resolve(config, "vlm"),
        resolve("research", provider, "research_extract" if read_only else "research_execute"),
        None, "C1", "1.1", read_only=read_only,
    )
    return resolve_contract(config, request)


@pytest.mark.parametrize("missing", ["filesystem.deny_read", "filesystem.write_scope",
                                     "network.policy", "mcp.allowlist"])
def test_codex_preflight_rejects_unenforceable_required_capability(config, missing):
    with pytest.raises(CapabilityUnavailable, match=missing):
        preflight(config, _contract(config), "codex_cli", unavailable={missing})


def test_codex_profile_carves_out_secret_reads_and_scopes_writes(config):
    contract = _contract(config)
    profile = preflight(config, contract, "codex_cli")

    assert profile.filesystem[":root"] == "read"
    assert profile.filesystem[str(denied_reads(config)[0])] == "deny"
    assert profile.filesystem[str(config.research_root / "vlm")] == "write"
    # 自分のデータを読む研究のコマンドは、どこへでも出られる（接続先の一覧も通信の中継も使わない）
    assert profile.network_open
    assert "permissions.kei_agent_scoped.network.enabled=true" in profile.config_overrides
    assert not any("network.domains" in item or "network_proxy" in item for item in profile.config_overrides)
    assert 'permissions.kei_agent_scoped.extends=":read-only"' in profile.config_overrides
    assert "~/.codex" in DEFAULT_DENY_READ                         # Codex の認証の置き場所も既定で読ませない
    # 読むだけの回は、どこにも書けない
    assert "write" not in preflight(config, _contract(config, read_only=True), "codex_cli").filesystem.values()


@pytest.mark.parametrize("agent,case,network", [("course", "course_explain", True),
                                                 ("work", "work_single_source", False)])
def test_connector_agents_write_their_workspace_and_reach_out_only_with_own_data(config, agent, case, network):
    """連携を使う担当も、作業場に書けて、秘密情報の置き場所は読めない。コマンドの通信は、自分のデータを読む
    担当（course）はどこへでも、会社のデータを読む担当（work）はどこへも出さない。"""
    workspace = themes.agent_workspace(config, agent)
    contract = resolve_contract(config, runner.ExecutionRequest(workspace, resolve(agent, "codex", case),
                                                                None, "C1", "1.1"))
    profile = preflight(config, contract, "codex_cli")

    assert profile.filesystem[":root"] == "read"
    assert profile.filesystem[str(workspace.cwd)] == "write"
    assert all(profile.filesystem[str(denied)] == "deny" for denied in denied_reads(config, agent))
    assert profile.network_open is network
    enabled = "true" if network else "false"
    assert f"permissions.kei_agent_scoped.network.enabled={enabled}" in profile.config_overrides


def test_only_agents_with_commands_reach_out(config):
    """コマンドの通信は sandbox の中のコマンドにだけ効く。コマンドを持たない振り分けには通信させない。"""
    request = runner.ExecutionRequest(themes.resolve(config, "vlm"), resolve("router", "codex", UseCase.ROUTING),
                                      None, "C1", "1.1")
    assert not preflight(config, resolve_contract(config, request), "codex_cli").network_open

