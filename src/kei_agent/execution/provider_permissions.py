"""Claude / Codex に渡す権限を、実行条件（制限の表）から導出する。強制できなければ実行を拒否する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kei_agent.configuration.config import Config
from kei_agent.execution import guard
from kei_agent.execution.execution_contract import ExecutionContract

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
    filesystem: dict[str, str]
    network_domains: dict[str, str]
    config_overrides: tuple[str, ...]
    # コマンドの通信がどこへでも出られるか
    network_open: bool = False


def _toml_map(values: dict[str, str]) -> str:
    return "{" + ",".join(f"{json.dumps(key)}={json.dumps(value)}" for key, value in values.items()) + "}"


def _overrides(filesystem: dict[str, str], network_open: bool) -> tuple[str, ...]:
    return (
        f'default_permissions={json.dumps(PROFILE_NAME)}',
        f'permissions.{PROFILE_NAME}.extends=":read-only"',
        f"permissions.{PROFILE_NAME}.filesystem={_toml_map(filesystem)}",
        f"permissions.{PROFILE_NAME}.network.enabled={'true' if network_open else 'false'}",
    )


def _filesystem(config: Config, contract: ExecutionContract) -> dict[str, str]:
    policy, workspace = contract.policy, contract.workspace
    assert workspace.cwd is not None
    if policy.files == "none":
        # 連携だけを使う担当。個人のファイル（ホームの下）は読ませず、自分の作業場と skill の置き場だけを読む。
        # PC 全体を拒否すると、Codex が自分を sandbox の中で起動できない（作業場の AGENTS.md を読むため）
        filesystem = {":root": "read", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny",
                      str(Path.home()): "deny", str(workspace.cwd): "read"}
        for skills in contract.skill_dirs:
            filesystem[str(skills)] = "read"
        # 越えてはいけない線（秘密情報・アカウントのフォルダ・ほかのアカウントの作業場）は、どの担当にも掛ける
        for denied in guard.denied_reads(config, policy.name):
            filesystem[str(denied)] = "deny"
        return filesystem
    filesystem = {":root": "read", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny"}
    for root in guard.read_roots(config, workspace):
        filesystem[str(root)] = "read"
    if policy.files == "write":
        filesystem[str(workspace.cwd)] = "write"
        for root in config.allow_write:
            filesystem[str(root)] = "write"
    for denied in guard.denied_reads(config, policy.name):
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

    # コマンドの通信。外へ出てよい実行役（自分のデータ）はどこへでも、会社のデータを読む実行役はどこへも出さない
    network_open = contract.policy.network and contract.policy.shell
    network_domains: dict[str, str] = {}
    filesystem = _filesystem(config, contract)

    if "mcp.allowlist" in contract.capabilities and not config.notion_gateway_url:
        raise CapabilityUnavailable("Notion gateway is not configured")
    if runtime == "claude_cli":
        return PermissionProfile(filesystem, network_domains, (), network_open)
    return PermissionProfile(filesystem, network_domains, _overrides(filesystem, network_open), network_open)
