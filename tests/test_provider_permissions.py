from dataclasses import replace

import pytest

from kei_agent import runner, themes
from kei_agent.execution_contract import resolve_contract
from kei_agent.guard import DEFAULT_DENY_READ
from kei_agent.model_policy import UseCase, resolve
from kei_agent.provider_permissions import CapabilityUnavailable, connector_profile, preflight


def _contract(config, *, provider="codex", read_only=False):
    request = runner.ExecutionRequest(
        themes.resolve(config, "vlm"),
        resolve("research", provider, UseCase.RESEARCH_EXTRACT if read_only else UseCase.RESEARCH_EXECUTE),
        None, "C1", "1.1", read_only=read_only,
    )
    return resolve_contract(config, request)


@pytest.mark.parametrize("missing", ["filesystem.deny_read", "filesystem.write_scope",
                                     "network.domain_allowlist", "mcp.allowlist"])
def test_codex_preflight_rejects_unenforceable_required_capability(config, missing):
    with pytest.raises(CapabilityUnavailable, match=missing):
        preflight(config, _contract(config), "codex_cli", unavailable={missing})


def test_codex_profile_carves_out_secret_reads_and_scopes_writes(config):
    contract = _contract(config)
    profile = preflight(config, contract, "codex_cli")

    assert profile.filesystem[":root"] == "read"
    assert profile.filesystem[str(config.deny_read[0])] == "deny"
    assert profile.filesystem[str(config.research_root / "vlm")] == "write"
    assert profile.network_domains == {"export.arxiv.org": "allow"}
    assert "features.network_proxy=true" in profile.config_overrides
    assert 'permissions.kei_agent_scoped.extends=":read-only"' in profile.config_overrides


def test_read_only_contract_has_no_write_grants(config):
    profile = preflight(config, _contract(config, read_only=True), "codex_cli")

    assert "write" not in profile.filesystem.values()


def test_codex_profile_rejects_disallowed_domain(config):
    bad_config = replace(config, allowed_domains=("*",))
    with pytest.raises(CapabilityUnavailable, match="domain"):
        preflight(bad_config, _contract(bad_config), "codex_cli")


def test_codex_connector_profile_has_no_local_writes_or_direct_network(config, tmp_path):
    profile = connector_profile(tmp_path / "course")

    assert profile.filesystem[str(tmp_path / "course")] == "read"
    assert "write" not in profile.filesystem.values()
    assert profile.network_domains == {}
    assert "permissions.kei_agent_scoped.network.enabled=false" in profile.config_overrides


def test_default_secret_denials_cover_codex_auth():
    assert "~/.codex" in DEFAULT_DENY_READ
