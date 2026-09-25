from dataclasses import replace

import pytest

from kei_agent import router, runner, themes
from kei_agent.execution_contract import resolve_contract
from kei_agent.model_policy import UseCase, resolve
from kei_agent.themes import ChannelKind, Workspace


@pytest.mark.parametrize("actor,case,provider,model,effort,prompt_name,has_skills", [
    ("router", UseCase.ROUTING, "codex", "gpt-6-luna", "low", "router.md", False),
    ("router", UseCase.ROUTING, "claude", "claude-haiku-4-5", "", "router.md", False),
    ("research", UseCase.RESEARCH_EXECUTE, "codex", "gpt-6-sol", "high", "system.md", True),
    ("research", UseCase.RESEARCH_EXECUTE, "claude", "claude-sonnet-5", "high", "system.md", True),
    ("course", UseCase.COURSE_EXPLAIN, "codex", "gpt-6-luna", "medium", "course.md", True),
    ("course", UseCase.COURSE_EXPLAIN, "claude", "claude-sonnet-5", "medium", "course.md", True),
    ("work", UseCase.WORK_SINGLE_SOURCE, "codex", "gpt-6-luna", "medium", "work.md", True),
    ("work", UseCase.WORK_SINGLE_SOURCE, "claude", "claude-sonnet-5", "medium", "work.md", True),
])
def test_agent_provider_contract_matrix(config, actor, case, provider, model, effort, prompt_name, has_skills):
    workspace = (router.workspace(config) if actor == "router" else
                 themes.resolve(config, "vlm") if actor == "research" else
                 Workspace(actor, ChannelKind.COURSE if actor == "course" else ChannelKind.WORK,
                           config.course_root if actor == "course" else config.agent_root / "work"))
    contract = resolve_contract(config, runner.ExecutionRequest(
        workspace, resolve(actor, provider, case), None, "C1", "1.1"))

    assert (contract.recipe.model, contract.recipe.reasoning_effort) == (model, effort)
    assert contract.prompt_text == (config.repo_root / "prompts" / prompt_name).read_text(encoding="utf-8")
    assert len(contract.prompt_version) == 12
    assert (contract.skill_dir is not None) is has_skills
    assert {"filesystem.read", "filesystem.deny_read", "network.domain_allowlist"} <= contract.capabilities


def test_research_contract_uses_workspace_prompt_and_agent_skills(config):
    workspace = themes.resolve(config, "vlm")
    request = runner.ExecutionRequest(
        workspace, resolve("research", "codex", UseCase.RESEARCH_EXECUTE), None, "C1", "1.1"
    )

    contract = resolve_contract(config, request)

    prompt_path = workspace.system_prompt or config.system_prompt_path
    assert contract.recipe == request.recipe
    assert contract.prompt_text == prompt_path.read_text(encoding="utf-8")
    assert contract.skill_dir == config.agent_plugin_dir("research") / "skills"
    assert len(contract.prompt_version) == 12
    assert "mcp.allowlist" in contract.capabilities


def test_router_contract_does_not_inherit_research_skills_or_write(config):
    request = runner.ExecutionRequest(
        router.workspace(config), resolve("router", "codex", UseCase.ROUTING), None, "C1", "1.1"
    )

    contract = resolve_contract(config, request)

    assert contract.skill_dir is None
    assert contract.read_only
    assert "filesystem.write_scope" not in contract.capabilities


def test_read_only_research_contract_does_not_request_write_or_notion(config):
    request = runner.ExecutionRequest(
        themes.resolve(config, "vlm"), resolve("research", "codex", UseCase.RESEARCH_EXTRACT),
        None, "C1", "1.1", read_only=True,
    )

    contract = resolve_contract(config, request)

    assert contract.read_only
    assert "filesystem.write_scope" not in contract.capabilities
    assert "mcp.allowlist" not in contract.capabilities


def test_research_skill_change_invalidates_the_session_version(config, tmp_path):
    repo = tmp_path / "repo"
    prompt = repo / "prompts" / "system.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Kei Agent の指示")
    skill = repo / "plugin" / "research" / "skills" / "run" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("元の手順")
    local = replace(config, repo_root=repo)
    first = runner.system_prompt_version(local)
    request = runner.ExecutionRequest(
        themes.resolve(local, "vlm"), resolve("research", "codex", UseCase.RESEARCH_EXECUTE),
        None, "C1", "1.1")
    contract_version = resolve_contract(local, request).prompt_version
    skill.write_text("更新した手順")
    assert runner.system_prompt_version(local) != first
    assert resolve_contract(local, request).prompt_version != contract_version
