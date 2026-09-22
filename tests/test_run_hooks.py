from dataclasses import replace
from types import SimpleNamespace

import pytest

from kei_agent import run_hooks, runner, themes


def test_preflight_rejects_connectors_outside_the_agents_policy():
    """work に W&B のような担当外 connector を渡す変更を捕捉する。"""
    context = run_hooks.RunContext(
        agent="work", provider="codex", workspace_kind="work", model="gpt-5.3-codex",
    )

    with pytest.raises(run_hooks.ConnectorPolicyError, match="許可されていません"):
        run_hooks.preflight(context, frozenset({"wandb"}), frozenset({"wandb"}))


def test_preflight_rejects_sharepoint_and_course_outlook():
    work = run_hooks.RunContext(agent="work", provider="codex", workspace_kind="work", model="")
    course = run_hooks.RunContext(agent="course", provider="codex", workspace_kind="course", model="")
    with pytest.raises(run_hooks.ConnectorPolicyError):
        run_hooks.preflight(work, frozenset({"sharepoint"}), frozenset({"sharepoint"}))
    with pytest.raises(run_hooks.ConnectorPolicyError):
        run_hooks.preflight(course, frozenset({"outlook_email"}), frozenset({"outlook_email"}))


def test_preflight_rejects_declared_connectors_missing_from_codex():
    """未接続 W&B を接続済みとみなして Codex を起動する変更を捕捉する。"""
    context = run_hooks.RunContext(
        agent="research", provider="codex", workspace_kind="theme", model="gpt-5.3-codex",
    )

    with pytest.raises(run_hooks.ConnectorPolicyError, match="Codex MCP に見つかりません"):
        run_hooks.preflight(context, frozenset({"wandb"}), frozenset())


def test_preflight_allows_a_declared_enabled_research_connector():
    """許可済みかつ検出済みの W&B connector を不必要に拒否する変更を捕捉する。"""
    context = run_hooks.RunContext(
        agent="research", provider="codex", workspace_kind="theme", model="gpt-5.3-codex",
    )

    run_hooks.preflight(context, frozenset({"wandb"}), frozenset({"wandb", "notion"}))


def test_post_run_exposes_only_minimal_outcome_metadata():
    """post-run hook に prompt や応答本文を渡す変更を捕捉する。"""
    context = run_hooks.RunContext(
        agent="research", provider="codex", workspace_kind="theme", model="gpt-5.3-codex",
    )
    outcome = run_hooks.RunOutcome(context=context, duration_ms=42, is_error=False, session_id="thread-1")
    seen: list[run_hooks.RunOutcome] = []

    run_hooks.post_run(outcome, observer=seen.append)

    assert seen == [outcome]
    assert not hasattr(outcome, "prompt")
    assert not hasattr(outcome, "text")


async def test_runner_rejects_a_missing_codex_connector_before_starting(config, tmp_path, monkeypatch):
    """未接続 connector でも Codex 子プロセスを起動する変更を捕捉する。"""
    marker = tmp_path / "started"
    fake = tmp_path / "fake-codex.sh"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    profile = SimpleNamespace(provider="codex", model="", reasoning_effort="high", connectors=frozenset({"wandb"}))
    config = replace(config, codex_bin=str(fake), agent_profiles={"research": profile})
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)

    async def no_connectors(_: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(runner, "discover_mcp_names", no_connectors)

    with pytest.raises(run_hooks.ConnectorPolicyError, match="Codex MCP に見つかりません"):
        await runner.run_claude(config, ws, "調べて", None, "C1", "1.1")

    assert not marker.exists()
