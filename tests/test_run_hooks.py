from dataclasses import replace
from types import SimpleNamespace

import pytest

from kei_agent import run_hooks, runner, themes
from kei_agent.model_policy import UseCase, resolve


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


async def test_runner_rejects_user_config_mcp_hidden_by_ignore_user_config(config, tmp_path, monkeypatch):
    """ユーザー設定にある MCP を、隔離起動中にも使えると誤認しない。"""
    marker = tmp_path / "started"
    fake = tmp_path / "fake-codex.sh"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    profile = SimpleNamespace(provider="codex", model="", reasoning_effort="high", connectors=frozenset({"wandb"}))
    config = replace(config, codex_bin=str(fake), agent_profiles={"research": profile})
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)

    async def verified(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "verify_codex_profile", verified)

    with pytest.raises(run_hooks.ConnectorPolicyError, match="Codex MCP に見つかりません"):
        await runner.run_model(config, runner.ExecutionRequest(
            ws, resolve("research", "codex", UseCase.RESEARCH_EXECUTE), None, "C1", "1.1"), "調べて")

    assert not marker.exists()


async def test_runner_accepts_the_scoped_research_notion_gateway(config, tmp_path):
    """実行時だけ注入する research-notion は preflight で許可する。"""
    fake = tmp_path / "fake-codex.sh"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"t1\"}' '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"OK\"}}' '{\"type\":\"turn.completed\"}'\n")
    fake.chmod(0o755)
    profile = SimpleNamespace(provider="codex", model="", reasoning_effort="high", connectors=frozenset({"research-notion"}))
    config = replace(config, codex_bin=str(fake), agent_profiles={"research": profile})
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)

    result = await runner.run_model(config, runner.ExecutionRequest(
        ws, resolve("research", "codex", UseCase.RESEARCH_EXECUTE), None, "C1", "1.1"), "調べて")

    assert result.text == "OK"
