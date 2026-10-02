"""仕事のモジュールのプロジェクトのチャンネル（#work-<名前>）: 作業場で、会社のアカウントでコードを書く。

#3-work（メール・予定・資料を読んで答える）と同じ担当・同じアカウントで動く。
"""

import subprocess
from dataclasses import replace

import pytest
from fakes import make_home

from kei_agent.configuration import agents_table
from kei_agent.configuration.config import load_config
from kei_agent.execution.agent_policy import policy_of
from kei_agent.framework import modules
from kei_agent.workspaces import themes
from kei_agent.workspaces.themes import ChannelKind
from kei_agent_modules.work import module as work


def _config(tmp_path, folder=""):
    home = make_home(tmp_path, agents=[{"module": "work", "folder": folder}, "research"])
    return load_config(env={"KEI_AGENT_HOME": str(home)})


def test_a_work_dash_channel_is_a_project_under_the_work_folder(tmp_path):
    config = _config(tmp_path, folder=str(tmp_path / "work"))
    ws = themes.resolve(config, "3-work-billing")
    assert (ws.kind, ws.module, ws.channel_name) == (ChannelKind.PROJECT, "work", "work-billing")
    assert ws.cwd == (tmp_path / "work").resolve() / "billing"
    # コードを書く作業場なので、上限は担当の短い上限（#3-work の読む回）ではなく config.toml のもの
    assert ws.timeout_minutes == config.run_timeout_minutes
    # #3-work は今までどおり、仕事のモジュールのチャンネル。ほかは研究テーマ
    assert themes.resolve(config, "3-work").kind is ChannelKind.MODULE
    assert themes.resolve(config, "3-work").module == "work"
    assert themes.resolve(config, "work-").kind is ChannelKind.THEME
    assert themes.resolve(config, "vlm").module == "research"
    # 既存のリポジトリを選んだプロジェクト（themes.toml）は、研究テーマの一覧に入れない
    repo = tmp_path / "repo"
    repo.mkdir()
    themes.save_place(config, "work-billing", repo)
    assert themes.resolve(config, "work-billing").cwd == repo and themes.resolve(config, "work-billing").external
    assert "work-billing" not in themes.all_themes(config)


def test_the_work_agents_own_place_is_not_the_projects_folder(tmp_path):
    """#3-work の作業場は状態の置き場の下。プロジェクトを並べる場所（~/work）と混ぜない。"""
    config = _config(tmp_path, folder=str(tmp_path / "work"))
    own = themes.agent_workspace(config, "work")
    assert own.cwd == config.state_dir / "agents" / "work"
    assert config.module_workspace("work") == (tmp_path / "work").resolve()
    assert "# 仕事のチャンネル" in (own.cwd / "AGENTS.md").read_text()
    assert not (tmp_path / "work").exists()                  # 担当の作業場を作っても、プロジェクトの場所は触らない


def test_the_work_row_splits_its_channels_by_how_they_are_written():
    """#3-work とプロジェクトの2種類。agents.csv の work の行は、"work-*" の形をプロジェクトに、ほかを #3-work にする。
    書かなかった種類は既定のまま（#3-work だけを書いても、プロジェクトのチャンネルは消えない）。"""
    header = "module,enabled,channels,folder,engine,model,effort\n"
    parsed = agents_table.parse(header + "work,true,work,~/work,,,\n")
    assert parsed["channels"] == {"work": ["work"]} and parsed["folders"] == {"work": "~/work"}
    parsed = agents_table.parse(header + "work,true,job job-* ,,,,\n")
    assert parsed["channels"] == {"work": ["job"], "project": ["job-*"]}
    built = agents_table.build(["work"], channels={"work": ["job"], "project": ["job-*"]})
    assert "work,true,job job-*," in built and agents_table.parse(built)["channels"] == {
        "work": ["job"], "project": ["job-*"]}


def test_a_project_keeps_kei_agent_files_out_of_its_git(tmp_path):
    config = _config(tmp_path, folder=str(tmp_path / "work"))
    ws = themes.resolve(config, "work-billing")
    ws.cwd.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(ws.cwd)], check=True)
    themes.ensure_workspace(ws)
    assert "# プロジェクト: work-billing" in (ws.cwd / "AGENTS.md").read_text()
    assert (ws.cwd / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    exclude = (ws.cwd / ".git" / "info" / "exclude").read_text().splitlines()
    assert {"/.kei-agent/", "/inputs/", "/outputs/"} <= set(exclude)
    themes.ensure_workspace(ws)
    assert (ws.cwd / ".git" / "info" / "exclude").read_text().splitlines() == exclude   # 二度は書かない
    # 既存のリポジトリの AGENTS.md・CLAUDE.md は動かさない
    theirs = replace(ws, cwd=tmp_path / "theirs", external=True)
    theirs.cwd.mkdir()
    (theirs.cwd / "CLAUDE.md").write_text("# その人の前提\n")
    themes.ensure_workspace(theirs)
    assert not (theirs.cwd / "AGENTS.md").exists()


def test_work_writes_code_with_the_work_account_but_never_goes_outside():
    """仕事は会社のデータを読む実行役。作業場に書いてコマンドを使い、Microsoft 365 を読むが、Web もコマンドの通信も無い。"""
    spec = modules.builtin()["work"]
    assert spec.prefixes == {"work-": "project"} and spec.actor.data == "company"
    assert "workdev" not in modules.builtin()
    policy = policy_of("work")
    assert (policy.files, policy.shell, policy.web, policy.network, policy.notion) == (
        "write", True, False, False, "none")
    assert policy.connectors and policy.plugin
    # 重さ → 用途。コードの作業は work_execute・work_design
    assert spec.actor.weights == {"light": "work_single_source", "normal": "work_execute", "deep": "work_design"}
    assert {"work_execute", "work_design", "work_single_source"} <= {u.name for u in spec.actor.use_cases}


def test_only_project_channels_go_to_the_workspace():
    assert work.is_project("work-billing", ("work-*",)) and work.is_project("3-work-billing", ("work-*",))
    assert not work.is_project("work", ("work-*",)) and not work.is_project("work-", ("work-*",))
    assert not work.is_project("research-overview", ("work-*",))


def test_only_a_head_ending_in_a_dash_can_be_a_pattern(tmp_path):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "module.toml").write_text('api = 1\nname = "bad"\n[channels]\nx = ["wo*rk"]\n')
    with pytest.raises(modules.ModuleError, match="work-\\*"):
        modules.load_spec(folder)


async def test_a_project_channel_goes_to_the_work_agent_in_its_folder(module_kit):
    pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
    kit = module_kit("work")
    kit.ai.answer("テストを直したよ")
    ts = await kit.message("テストを直して", channel="project")
    call, = kit.ai.calls
    assert call["actor"] == "work" and call["cwd"] == kit.config.module_workspace("work") / "demo"
    assert kit.thread(ts)[-1] == "テストを直したよ"
