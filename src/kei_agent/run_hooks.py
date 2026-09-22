"""実行器に依存しない、最小の実行前後フック。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

CONNECTOR_POLICY: dict[str, frozenset[str]] = {
    "research": frozenset({"research-notion", "wandb"}),
    "course": frozenset({"notion", "box"}),
    "work": frozenset({"microsoft-365"}),
}


class ConnectorPolicyError(ValueError):
    """Codex connector の宣言が担当範囲または実接続と一致しない。"""


@dataclass(frozen=True, slots=True)
class RunContext:
    agent: str
    provider: str
    workspace_kind: str
    model: str


@dataclass(frozen=True, slots=True)
class RunOutcome:
    context: RunContext
    duration_ms: int
    is_error: bool
    session_id: str | None


def preflight(
    context: RunContext,
    declared_connectors: frozenset[str],
    configured_connectors: frozenset[str],
) -> None:
    """Codex に渡す connector が担当範囲と実設定の両方にあることを確かめる。"""
    if context.provider != "codex":
        return
    allowed = CONNECTOR_POLICY.get(context.agent, frozenset())
    forbidden = sorted(declared_connectors - allowed)
    if forbidden:
        raise ConnectorPolicyError(
            f"{context.agent} agent では connector が許可されていません: {', '.join(forbidden)}"
        )
    missing = sorted(declared_connectors - configured_connectors)
    if missing:
        raise ConnectorPolicyError(f"宣言した connector が Codex MCP に見つかりません: {', '.join(missing)}")


def post_run(outcome: RunOutcome, observer: Callable[[RunOutcome], None] | None = None) -> None:
    """安全なメタデータだけを受け取る post-run の差し込み口。"""
    if observer:
        observer(outcome)
