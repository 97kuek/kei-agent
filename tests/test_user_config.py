"""利用者のフォルダ（~/.config/kei-agent/）: 設定・プロフィール・指示書の差し替え・秘密情報の置き場所。

リポジトリには例（config.example.toml、profile.example.md）だけを置く（docs/extensibility.md）。
"""

import subprocess
import sys

import pytest
from fakes import write_agents

from kei_agent.configuration.config import ConfigError, load_config
from kei_agent.execution.execution_contract import prompt_text, prompt_version
from kei_agent.execution.guard import denied_reads


def _home(tmp_path, config: str = "", profile: str | None = None, prompts: dict[str, str] | None = None):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")
    if profile is not None:
        (home / "profile.md").write_text(profile, encoding="utf-8")
    for name, text in (prompts or {}).items():
        (home / "prompts").mkdir(exist_ok=True)
        (home / "prompts" / name).write_text(text, encoding="utf-8")
    return home


def test_config_comes_from_the_user_folder_and_says_how_to_start_without_one(tmp_path):
    home = _home(tmp_path)
    write_agents(home, [{"module": "overview", "notion": "abc"}])
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.user_dir == home.resolve() and config.notion.hub_home == "abc"
    with pytest.raises(ConfigError, match="config.example.toml を写して"):
        load_config(env={"KEI_AGENT_HOME": str(tmp_path / "nowhere")})
    # KEI_AGENT_CONFIG で別のファイルも読める（利用者のフォルダは、そのファイルのあるところ）
    other = tmp_path / "other.toml"
    other.write_text("")
    assert load_config(env={"KEI_AGENT_CONFIG": str(other)}).user_dir == tmp_path.resolve()


def test_secrets_folder_is_configurable_and_never_readable_by_the_ai(tmp_path):
    home = _home(tmp_path)
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.secrets_dir == (home / "secrets").resolve() and config.secrets_dir in denied_reads(config)
    chosen = tmp_path / "local"
    home_b = tmp_path / "b"
    home_b.mkdir()
    # deny_read を自分で書いていても、選んだ秘密情報の置き場所は必ず読ませない
    (home_b / "config.toml").write_text(f'[paths]\nsecrets = "{chosen}"\n\n[sandbox]\ndeny_read = ["~/.ssh"]\n')
    config = load_config(env={"KEI_AGENT_HOME": str(home_b)})
    assert config.secrets_dir == chosen.resolve() and chosen.resolve() in denied_reads(config)
    (home_b / "config.toml").write_text('[paths]\nsecret = "x"\n')
    with pytest.raises(ConfigError, match=r"\[paths\] に知らないキー"):
        load_config(env={"KEI_AGENT_HOME": str(home_b)})


def test_notion_homes_are_written_in_the_notion_column(tmp_path):
    """Notion のホームは担当の表の notion 列（共通ホームは overview の行）。ID はハイフンを外して比べる。"""
    home = _home(tmp_path)
    (home / "agents.csv").write_text("module,enabled,channels,notion,engine,model,effort\n"
                                     "overview,true,,HUB-1,,,\ncourse,true,,AAAA-BBBB,,,\nknowledge,true,,CCCC,,,\n")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.notion.hub_home == "hub1"
    assert config.notion.client_homes() == {"course": "aaaabbbb", "knowledge": "cccc"}
    (home / "agents.csv").write_text("module,enabled,channels,notion,engine,model,effort\nrouter,true,,X,,,\n")
    with pytest.raises(ConfigError, match="notion を書けません"):
        load_config(env={"KEI_AGENT_HOME": str(home)})
    # config.toml の [notion] は、表に書くよう知らせる
    (home / "config.toml").write_text('[notion]\ncourse_home = "a"\n')
    with pytest.raises(ConfigError, match="Notion のホームは notion 列"):
        load_config(env={"KEI_AGENT_HOME": str(home)})


def test_profile_is_added_to_conversation_prompts_but_not_to_json_only_ones(tmp_path):
    """話し方や所属はプロフィールから。振り分け・選別のように JSON だけを返す係には足さない。"""
    home = _home(tmp_path, profile="# プロフィール\n\n<!-- 書き方の説明 -->\n## 話し方\n\n- 一人称は「僕」\n")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    course = prompt_text(config, config.prompt_file("course.md"))
    assert course.rstrip().endswith("## 依頼者のプロフィール\n\n## 話し方\n\n- 一人称は「僕」")
    assert "# プロフィール" not in course and "書き方の説明" not in course     # 題とコメントは差し込まない
    # JSON だけを返す係は、作業場で差し込まないと決める（振り分け・分類の作業場と、知識の選別・要約の回）
    from kei_agent.conversation import router
    ws = router.workspace(config)
    assert ws.profile is False and "依頼者のプロフィール" not in prompt_text(config, ws.system_prompt, ws.profile)
    digest = config.prompt_file("knowledge-digest.md", module="knowledge")
    assert digest == config.repo_root / "modules" / "knowledge" / "knowledge-digest.md" and digest.is_file()
    assert "依頼者のプロフィール" not in prompt_text(config, digest, profile=False)
    # プロフィールを変えたら、指示の版も変わる（古い会話を、前の話し方のまま続けない）
    before = prompt_version(config, "course")
    (home / "profile.md").write_text("## 話し方\n\n- 一人称は「私」\n", encoding="utf-8")
    assert prompt_version(config, "course") != before
    # 例のプロフィールをそのまま写しても、書き方の説明は指示書に入らない
    (home / "profile.md").write_text((config.repo_root / "profile.example.md").read_text(encoding="utf-8"),
                                     encoding="utf-8")
    text = load_config(env={"KEI_AGENT_HOME": str(home)}).profile_text
    assert text.startswith("## 話し方") and "<!--" not in text and "写して書き換える" not in text


def test_a_prompt_can_be_replaced_as_a_whole(tmp_path):
    home = _home(tmp_path, prompts={"course.md": "# 自分の大学エージェント\n"})
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.prompt_file("course.md") == home.resolve() / "prompts" / "course.md"
    assert config.prompt_file("work.md") == config.repo_root / "prompts" / "work.md"
    assert prompt_text(config, config.prompt_file("course.md")) == "# 自分の大学エージェント\n"
    # モジュールの指示書はモジュールのフォルダにある。同じ名前を利用者のフォルダに置けば、それも差し替えられる
    assert config.prompt_file("knowledge.md", module="knowledge") == config.repo_root / "modules" / "knowledge" / "knowledge.md"
    (home / "prompts" / "knowledge.md").write_text("# 自分の知識の担当\n", encoding="utf-8")
    assert config.prompt_file("knowledge.md", module="knowledge") == home.resolve() / "prompts" / "knowledge.md"


def test_launch_scripts_can_ask_where_the_secrets_are(tmp_path):
    """起動スクリプトは、本体を起動する前に秘密情報の置き場所を聞く。設定が読めなくても既定の場所を答える。"""
    home = _home(tmp_path, f'[paths]\nsecrets = "{tmp_path / "local"}"\n')

    def ask(env_home):
        return subprocess.run([sys.executable, "-m", "kei_agent.configuration.paths", "secrets"], capture_output=True, text=True,
                              check=True, env={"KEI_AGENT_HOME": str(env_home), "PATH": "/usr/bin:/bin"}).stdout.strip()

    assert ask(home) == str((tmp_path / "local").resolve())
    assert ask(tmp_path / "nowhere") == str((tmp_path / "nowhere").resolve() / "secrets")


def test_tests_never_point_at_the_real_state(tmp_path):
    """置き場所を書かない設定でも、テストの中では本物の ~/.local/state/kei-agent や ~/research を指さない（conftest）。"""
    from pathlib import Path

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    for path in (config.state_dir, config.research_root, config.agent_root, config.course_root, config.db_path):
        assert not path.is_relative_to(Path.home() / ".local" / "state")
        assert path not in (Path.home() / "research", Path.home() / "kei-agent", Path.home() / "course")
