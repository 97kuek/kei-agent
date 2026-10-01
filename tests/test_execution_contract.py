from dataclasses import replace

import pytest

from kei_agent import router, runner, themes
from kei_agent.execution_contract import prompt_version, resolve_contract
from kei_agent.model_policy import UseCase, resolve

ROUTER = {"filesystem.deny_read", "filesystem.read"}
RESEARCH = {"filesystem.deny_read", "filesystem.read", "filesystem.write_scope", "network.domain_allowlist",
            "mcp.allowlist"}
COURSE = {"filesystem.deny_read", "mcp.allowlist", "app.allowlist"}
WORK = {"filesystem.deny_read", "app.allowlist"}


@pytest.mark.parametrize("actor,case,provider,model,effort,prompt_name,has_skills,capabilities", [
    ("router", UseCase.ROUTING, "codex", "gpt-6-luna", "low", "router.md", False, ROUTER),
    ("router", UseCase.ROUTING, "claude", "claude-haiku-4-5", "", "router.md", False, ROUTER),
    ("research", "research_execute", "codex", "gpt-6-sol", "high", "research.md", True, RESEARCH),
    ("research", "research_execute", "claude", "claude-sonnet-5", "high", "research.md", True, RESEARCH),
    ("course", "course_explain", "codex", "gpt-6-luna", "medium", "course.md", True, COURSE),
    ("course", "course_explain", "claude", "claude-sonnet-5", "medium", "course.md", True, COURSE),
    ("work", "work_single_source", "codex", "gpt-6-luna", "medium", "work.md", True, WORK),
    ("work", "work_single_source", "claude", "claude-sonnet-5", "medium", "work.md", True, WORK),
])
def test_agent_provider_contract_matrix(config, actor, case, provider, model, effort, prompt_name, has_skills,
                                        capabilities):
    """Claude と Codex で、同じ担当は同じ指示書・skill・制限になる。"""
    workspace = (router.workspace(config) if actor == "router" else
                 themes.resolve(config, "vlm") if actor == "research" else
                 themes.agent_workspace(config, actor))
    request = runner.ExecutionRequest(workspace, resolve(actor, provider, case), None, "C1", "1.1")
    contract = resolve_contract(config, request)

    assert contract.recipe == request.recipe
    assert (contract.recipe.model, contract.recipe.reasoning_effort) == (model, effort)
    # 研究・大学・仕事の指示書はモジュールのフォルダ（modules/<名前>/）、本体の担当はリポジトリの prompts/
    folder = config.repo_root / (f"modules/{actor}" if actor in ("research", "course", "work") else "prompts")
    assert contract.prompt_text == (folder / prompt_name).read_text(encoding="utf-8")
    assert len(contract.prompt_version) == 12
    # skill は担当の plugin のもの。振り分け係は研究の skill も書き込みも受け継がない
    assert contract.skill_dir == (config.agent_plugin_dir(actor) / "skills" if has_skills else None)
    assert contract.read_only is (actor == "router")
    assert contract.capabilities == capabilities
    assert contract.policy.name == actor


def test_read_only_research_contract_does_not_request_write_and_reads_notion_only(config):
    request = runner.ExecutionRequest(
        themes.resolve(config, "vlm"), resolve("research", "codex", "research_extract"),
        None, "C1", "1.1", read_only=True,
    )

    contract = resolve_contract(config, request)

    assert contract.read_only
    assert "filesystem.write_scope" not in contract.capabilities
    assert contract.policy.notion == "read" and not contract.policy.shell


def test_an_actor_without_a_notion_home_gets_no_notion_tools(config):
    """config.toml の [notion] にホームが無い担当には、届かない Notion の道具（ゲートウェイの MCP）を渡さない。"""
    from kei_agent.configuration.config import NotionConfig

    request = runner.ExecutionRequest(themes.agent_workspace(config, "course"),
                                      resolve("course", "claude", "course_explain"), None, "C1", "1.1")
    assert resolve_contract(config, request).policy.notion == "write"
    contract = resolve_contract(replace(config, notion=NotionConfig(research_home="research-home")), request)
    assert contract.policy.notion == "none" and "mcp.allowlist" not in contract.capabilities
    # 合言葉も渡さない（届かない合言葉を AI の環境に置かない）
    assert runner.GATEWAY_AUTH_ENV not in runner.build_env(
        config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "master"}, "C1", "1.1", contract.policy)


def test_a_skill_change_invalidates_the_session_version(config, tmp_path):
    """指示書か skill が変わったら、古い会話を再開しない（会話の版が変わる）。"""
    from kei_agent.framework import modules

    folder = tmp_path / "modules" / "lab"
    (folder / "plugin" / ".claude-plugin").mkdir(parents=True)
    (folder / "plugin" / ".claude-plugin" / "plugin.json").write_text('{"name": "lab"}', encoding="utf-8")
    skill = folder / "plugin" / "skills" / "run" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("元の手順", encoding="utf-8")
    (folder / "lab.md").write_text("# 実験の担当\n", encoding="utf-8")
    (folder / "module.toml").write_text('api = 1\nname = "lab"\n[actor]\nprompt = "lab.md"\nplugin = true\n'
                                        '[use_cases.lab_run]\nclaude = { model = "claude-sonnet-5" }\n', encoding="utf-8")
    modules.register_user_modules(tmp_path / "modules")
    first = prompt_version(config, "lab")
    skill.write_text("更新した手順", encoding="utf-8")
    assert prompt_version(config, "lab") != first
    (folder / "lab.md").write_text("# 実験の担当（直した）\n", encoding="utf-8")
    assert len({first, prompt_version(config, "lab")}) == 2


def test_only_agents_that_reach_notion_get_the_shared_notion_skill(config):
    """既存のページの書式を保つ skill は、Notion を使える担当（ホームを書いたもの）にだけ渡す。"""
    from kei_agent.agent_policy import policy_of
    from kei_agent.configuration.config import NotionConfig
    from kei_agent.execution_contract import shared_skill_dirs

    notion = config.repo_root / "plugins" / "notion" / "skills"
    assert shared_skill_dirs(config, policy_of("research")) == (notion,)
    assert (notion / "keeping-notion-format" / "SKILL.md").is_file()
    assert shared_skill_dirs(config, policy_of("work")) == ()                 # Notion を持たない担当
    request = runner.ExecutionRequest(themes.resolve(config, "vlm"), resolve("research", "codex", "research_execute"),
                                      None, "C1", "1.1")
    assert resolve_contract(config, request).shared_skill_dirs == (notion,)
    contract = resolve_contract(replace(config, notion=NotionConfig()), request)
    assert contract.policy.notion == "none" and contract.shared_skill_dirs == ()  # ホームが無ければ渡さない
