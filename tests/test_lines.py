"""越えてはいけない線（docs/extensibility.md）は、Claude でも Codex でも同じ固さで守る。

秘密情報・鍵、アカウントのフォルダ、アカウントの違う担当の作業場は、どの担当のどの AI にも読ませない。
守れない AI では、始める前に止める。
"""

import asyncio
from dataclasses import replace

import pytest

from kei_agent.configuration.config import AgentProfile
from kei_agent.execution import guard, runner
from kei_agent.execution.execution_contract import resolve_contract
from kei_agent.execution.model_policy import resolve
from kei_agent.execution.provider_permissions import CapabilityUnavailable, preflight
from kei_agent.workspaces import themes

ACTORS = {"research": "research_execute", "course": "course_explain", "work": "work_single_source",
          "knowledge": "knowledge_answer", "workdev": "workdev_execute"}


def _accounts(config, tmp_path):
    """仕事と仕事の開発は会社、大学は個人のアカウント。仕事の開発の作業場は ~/work のつもりの場所。"""
    profiles = dict(config.agent_profiles)
    for name, folder in (("work", "claude-work"), ("workdev", "claude-work"), ("course", "claude-personal")):
        profiles[name] = replace(profiles[name], claude_account=str(tmp_path / folder))
    return replace(config, agent_profiles=profiles, modules=(*config.modules, "workdev"),
                   module_folders={"workdev": tmp_path / "work"})


def _request(config, actor, provider):
    ws = themes.resolve(config, "vlm") if actor == "research" else themes.agent_workspace(config, actor)
    return runner.ExecutionRequest(ws, resolve(actor, provider, ACTORS[actor]), None, "C", "1")


def _contract(config, actor, provider):
    return resolve_contract(config, _request(config, actor, provider))


@pytest.mark.parametrize("actor", ACTORS)
def test_claude_and_codex_deny_the_same_reads(config, tmp_path, actor):
    """ファイルを読まない担当（大学・仕事など）も含めて、読ませない場所は2つの AI で同じ。"""
    config = _accounts(config, tmp_path)
    expected = {str(p) for p in guard.denied_reads(config, actor)}
    claude = guard.build_settings(config, _contract(config, actor, "claude").workspace,
                                  _contract(config, actor, "claude").policy)
    assert set(claude["sandbox"]["filesystem"]["denyRead"]) == expected
    codex = preflight(config, _contract(config, actor, "codex"), "codex_cli").filesystem
    assert expected <= {path for path, mode in codex.items() if mode == "deny"}


@pytest.mark.parametrize("actor", ACTORS)
def test_claude_and_codex_write_only_where_the_actor_may(config, tmp_path, actor):
    """作業場の外に書かない。書けない担当は、Claude のコマンドからも作業場に書けない（Codex と同じ）。"""
    config = replace(_accounts(config, tmp_path), allow_write=(tmp_path / "cache",))
    contract = _contract(config, actor, "claude")
    claude = guard.build_settings(config, contract.workspace, contract.policy)["sandbox"]["filesystem"]
    codex = preflight(config, _contract(config, actor, "codex"), "codex_cli").filesystem
    cwd = str(contract.workspace.cwd)
    writable = {path for path, mode in codex.items() if mode == "write"}
    if contract.policy.files == "write":
        assert claude["allowWrite"] == [str(tmp_path / "cache")] and "denyWrite" not in claude
        assert {cwd, str(tmp_path / "cache")} <= writable
    else:
        assert claude["allowWrite"] == [] and claude["denyWrite"] == [cwd]
        assert writable == set()


def test_no_actor_brings_in_the_account_user_settings(config):
    """アカウントのユーザー設定（許可ルール・フック・MCP）は、連携を使う担当にも読ませない。"""
    for actor in ("work", "course", "research"):
        cmd = runner.build_command(config, _request(config, actor, "claude"))
        assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_accounts_and_other_accounts_workspaces_are_never_readable(config, tmp_path):
    """会社と個人のアカウントを混ぜない。ログインの情報があるアカウントのフォルダは、どの担当にも読ませない。"""
    config = _accounts(config, tmp_path)
    research = guard.denied_reads(config, "research")
    assert {tmp_path / "claude-work", tmp_path / "claude-personal"} <= set(research)
    assert tmp_path / "work" in research and config.research_root not in research   # 会社の作業場は読めない
    workdev = guard.denied_reads(config, "workdev")
    assert config.research_root in workdev and config.course_root in workdev          # 個人の作業場は読めない
    assert tmp_path / "work" not in workdev                                          # 自分の作業場は読める
    # 同じアカウントの担当どうし（仕事と仕事の開発）は、互いを塞がない
    assert tmp_path / "work" not in guard.denied_reads(config, "work")


async def test_claude_also_stops_before_running_when_the_lines_cannot_be_kept(config, monkeypatch):
    """始める前の確かめは Claude にも掛ける。守れないなら、AI を動かさずに理由を返す。"""
    def refuse(*args, **kwargs):
        raise CapabilityUnavailable("unavailable capability: filesystem.deny_read")
    monkeypatch.setattr(runner, "preflight", refuse)
    started = []

    async def never(*args, **kwargs):
        started.append(args)
        raise AssertionError("AI を起動してはいけない")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)
    result = await runner.run_model(config, runner.ExecutionRequest(
        ws, resolve("research", "claude", "research_execute"), None, "C", "1"), "調べて")
    assert result.is_error and result.failure_kind == "capability" and started == []


def test_module_efforts_must_be_ones_the_provider_knows(tmp_path):
    from kei_agent.framework import modules
    from kei_agent.framework.models import ModelCatalogError, check_module_recipes

    folder = tmp_path / "loud"
    folder.mkdir()
    (folder / "loud.md").write_text("# 担当\n")
    (folder / "module.toml").write_text('api = 1\nname = "loud"\n[actor]\nprompt = "loud.md"\n'
                                        '[use_cases.loud_answer]\nclaude = { model = "claude-sonnet-5", effort = "minimal" }\n')
    with pytest.raises(ModelCatalogError, match="effort minimal は使えません"):
        check_module_recipes(modules.load_spec(folder))


def test_profiles_without_accounts_add_nothing(config):
    """アカウントを書いていなければ、今までどおり（既定の読ませない場所と秘密情報の置き場所だけ）。"""
    plain = replace(config, agent_profiles={name: AgentProfile(provider="claude") for name in config.agent_profiles})
    assert guard.denied_reads(plain, "research") == guard.denied_reads(plain)
