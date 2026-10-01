"""仕事の開発のモジュール: 仕事のプロジェクトのチャンネル（#work-<名前>）の作業場で、会社のアカウントでコードを書く。"""

import subprocess
from dataclasses import replace

import pytest
from fakes import write_config

from kei_agent.configuration.config import load_config
from kei_agent.execution.agent_policy import policy_of
from kei_agent.framework import modules
from kei_agent.workspaces import themes
from kei_agent.workspaces.themes import ChannelKind


def _config(tmp_path, folder=""):
    home = tmp_path / "home"
    home.mkdir()
    write_config(home / "config.toml", 'modules = ["work", "workdev", "research"]\n')
    if folder:
        text = (home / "agents.csv").read_text().replace("workdev,true,,,", f"workdev,true,,{folder},")
        (home / "agents.csv").write_text(text)
    return load_config(env={"KEI_AGENT_HOME": str(home)})


def test_a_work_dash_channel_is_a_project_under_the_workdev_folder(tmp_path):
    config = _config(tmp_path, folder=str(tmp_path / "work"))
    ws = themes.resolve(config, "3-work-billing")
    assert (ws.kind, ws.module, ws.channel_name) == (ChannelKind.PROJECT, "workdev", "work-billing")
    assert ws.cwd == (tmp_path / "work").resolve() / "billing"
    # #3-work は今までどおり、仕事のモジュール（読むだけ）のチャンネル。ほかは研究テーマ
    assert themes.resolve(config, "3-work").kind is ChannelKind.MODULE
    assert themes.resolve(config, "work-").kind is ChannelKind.THEME
    assert themes.resolve(config, "vlm").module == "research"
    # 既存のリポジトリを選んだプロジェクト（themes.toml）は、研究テーマの一覧に入れない
    repo = tmp_path / "repo"
    repo.mkdir()
    themes.save_place(config, "work-billing", repo)
    assert themes.resolve(config, "work-billing").cwd == repo and themes.resolve(config, "work-billing").external
    assert "work-billing" not in themes.all_themes(config)


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


def test_workdev_writes_code_with_the_work_account_but_never_reads_mail():
    spec = modules.builtin()["workdev"]
    assert spec.prefixes == {"work-": "project"} and spec.secrets_file == "kei-agent-work.zsh"
    policy = policy_of("workdev")
    assert (policy.files, policy.shell, policy.web, policy.notion) == ("write", True, True, "none")
    # 外の文（メール）と、コマンド・Web の外へ出す口を、1回の実行に揃えない
    assert not policy.connectors and policy_of("work").connectors and not policy_of("work").shell


def test_only_a_head_ending_in_a_dash_can_be_a_pattern(tmp_path):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "module.toml").write_text('api = 1\nname = "bad"\n[channels]\nx = ["wo*rk"]\n')
    with pytest.raises(modules.ModuleError, match="work-\\*"):
        modules.load_spec(folder)


async def test_a_project_channel_goes_to_the_workdev_agent_in_its_folder(module_kit):
    pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
    kit = module_kit("workdev")
    kit.ai.answer("テストを直したよ")
    ts = await kit.message("テストを直して")
    call, = kit.ai.calls
    assert call["actor"] == "workdev" and call["cwd"] == kit.config.module_workspace("workdev") / "demo"
    assert kit.thread(ts)[-1] == "テストを直したよ"
