"""Claude / Codex に渡す権限を共通契約から導出し、不足時は実行を拒否する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kei_agent import guard
from kei_agent.config import Config
from kei_agent.execution_contract import ExecutionContract

Runtime = Literal["claude_cli", "codex_cli", "codex_app"]
PROFILE_NAME = "kei_agent_scoped"
_SUPPORTED = frozenset({
    "filesystem.read", "filesystem.deny_read", "filesystem.write_scope",
    "network.domain_allowlist", "mcp.allowlist", "app.allowlist",
})


class CapabilityUnavailable(RuntimeError):
    """要求された能力を選択 provider で安全に強制できない。"""


@dataclass(frozen=True)
class PermissionProfile:
    grants: frozenset[str]
    filesystem: dict[str, str]
    network_domains: dict[str, str]
    config_overrides: tuple[str, ...]


def _toml_map(values: dict[str, str]) -> str:
    return "{" + ",".join(f"{json.dumps(key)}={json.dumps(value)}" for key, value in values.items()) + "}"


def _overrides(filesystem: dict[str, str], network_domains: dict[str, str]) -> tuple[str, ...]:
    return (
        f'default_permissions={json.dumps(PROFILE_NAME)}',
        f'permissions.{PROFILE_NAME}.extends=":read-only"',
        f"permissions.{PROFILE_NAME}.filesystem={_toml_map(filesystem)}",
        f"permissions.{PROFILE_NAME}.network.enabled={'true' if network_domains else 'false'}",
        f"permissions.{PROFILE_NAME}.network.domains={_toml_map(network_domains)}",
        "features.network_proxy=true",
    )


def connector_profile(workspace: Path, skill_dir: Path | None = None) -> PermissionProfile:
    """App 連携専用。ローカルは skill だけ読め、書込みと直接通信はできない。"""
    filesystem = {":root": "deny", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny",
                  str(workspace): "read"}
    if skill_dir is not None:
        filesystem[str(skill_dir)] = "read"
    grants = frozenset({"filesystem.read", "filesystem.deny_read", "app.allowlist"})
    return PermissionProfile(grants, filesystem, {}, _overrides(filesystem, {}))


def preflight(config: Config, contract: ExecutionContract, runtime: Runtime,
              *, unavailable: frozenset[str] | set[str] = frozenset()) -> PermissionProfile:
    """必要な境界を強制できなければ profile を作らず止める。"""
    if runtime not in {"claude_cli", "codex_cli", "codex_app"}:
        raise CapabilityUnavailable(f"unknown runtime: {runtime}")
    missing = (contract.capabilities - _SUPPORTED) | (contract.capabilities & unavailable)
    if missing:
        raise CapabilityUnavailable("unavailable capability: " + ", ".join(sorted(missing)))
    if contract.workspace.cwd is None:
        raise CapabilityUnavailable("workspace path is missing")

    domains = tuple(dict.fromkeys((*config.allowed_domains, *contract.workspace.allowed_domains)))
    if any(not guard.valid_domain(domain, allow_wildcard=True) for domain in domains):
        raise CapabilityUnavailable("domain allowlist contains an unsupported pattern")
    network_domains = {domain: "allow" for domain in domains}

    filesystem = {":root": "read", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny"}
    for root in guard.read_roots(config, contract.workspace):
        filesystem[str(root)] = "read"
    if not contract.read_only:
        filesystem[str(contract.workspace.cwd)] = "write"
        for root in config.allow_write:
            filesystem[str(root)] = "write"
    for denied in config.deny_read:
        filesystem[str(denied)] = "deny"

    if runtime == "claude_cli":
        return PermissionProfile(contract.capabilities, filesystem, network_domains, ())

    if "mcp.allowlist" in contract.capabilities and not config.notion_gateway_url:
        raise CapabilityUnavailable("research Notion gateway is not configured")
    overrides = _overrides(filesystem, network_domains)
    return PermissionProfile(contract.capabilities, filesystem, network_domains, overrides)
