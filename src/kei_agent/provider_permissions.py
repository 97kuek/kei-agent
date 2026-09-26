"""Claude / Codex に渡す権限を、実行条件（制限の表）から導出する。強制できなければ実行を拒否する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kei_agent import guard
from kei_agent.config import Config
from kei_agent.execution_contract import ExecutionContract

Runtime = Literal["claude_cli", "codex_cli"]
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


def _filesystem(config: Config, contract: ExecutionContract) -> dict[str, str]:
    policy, workspace = contract.policy, contract.workspace
    assert workspace.cwd is not None
    if policy.files == "none":
        # 連携だけを使う担当。個人のファイル（ホームの下）は読ませず、自分の作業場と skill の置き場だけを読む。
        # PC 全体を拒否すると、Codex が自分を sandbox の中で起動できない（作業場の AGENTS.md を読むため）
        filesystem = {":root": "read", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny",
                      str(Path.home()): "deny", str(workspace.cwd): "read"}
        if contract.skill_dir is not None:
            filesystem[str(contract.skill_dir)] = "read"
        return filesystem
    filesystem = {":root": "read", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny"}
    for root in guard.read_roots(config, workspace):
        filesystem[str(root)] = "read"
    if policy.files == "write":
        filesystem[str(workspace.cwd)] = "write"
        for root in config.allow_write:
            filesystem[str(root)] = "write"
    for denied in config.deny_read:
        filesystem[str(denied)] = "deny"
    return filesystem


def preflight(config: Config, contract: ExecutionContract, runtime: Runtime,
              *, unavailable: frozenset[str] | set[str] = frozenset()) -> PermissionProfile:
    """必要な境界を強制できなければ profile を作らず止める。"""
    if runtime not in {"claude_cli", "codex_cli"}:
        raise CapabilityUnavailable(f"unknown runtime: {runtime}")
    missing = (contract.capabilities - _SUPPORTED) | (contract.capabilities & unavailable)
    if missing:
        raise CapabilityUnavailable("unavailable capability: " + ", ".join(sorted(missing)))
    if contract.workspace.cwd is None:
        raise CapabilityUnavailable("workspace path is missing")

    # 接続先の許可はコマンド（sandbox の中の Bash）にだけ効く。コマンドを持たない担当には通信させない
    domains = (tuple(dict.fromkeys((*config.allowed_domains, *contract.workspace.allowed_domains)))
               if contract.policy.shell else ())
    if any(not guard.valid_domain(domain, allow_wildcard=True) for domain in domains):
        raise CapabilityUnavailable("domain allowlist contains an unsupported pattern")
    network_domains = {domain: "allow" for domain in domains}
    filesystem = _filesystem(config, contract)

    if runtime == "claude_cli":
        return PermissionProfile(contract.capabilities, filesystem, network_domains, ())
    if "mcp.allowlist" in contract.capabilities and not config.notion_gateway_url:
        raise CapabilityUnavailable("Notion gateway is not configured")
    return PermissionProfile(contract.capabilities, filesystem, network_domains,
                             _overrides(filesystem, network_domains))
