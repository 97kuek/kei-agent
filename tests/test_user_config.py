"""利用者のフォルダ（~/.config/kei-agent/）: 設定・プロフィール・指示書の差し替え・秘密情報の置き場所。

リポジトリには例（config.example.toml、profile.example.md）だけを置く（docs/extensibility.md）。
"""

import subprocess
import sys

import pytest

from kei_agent.config import ConfigError, load_config
from kei_agent.execution_contract import prompt_text, prompt_version


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
    home = _home(tmp_path, '[notion]\nhub_home = "abc"\n')
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
    assert config.secrets_dir == (home / "secrets").resolve() and config.secrets_dir in config.deny_read
    chosen = tmp_path / "local"
    home_b = tmp_path / "b"
    home_b.mkdir()
    # deny_read を自分で書いていても、選んだ秘密情報の置き場所は必ず読ませない
    (home_b / "config.toml").write_text(f'[paths]\nsecrets = "{chosen}"\n\n[sandbox]\ndeny_read = ["~/.ssh"]\n')
    config = load_config(env={"KEI_AGENT_HOME": str(home_b)})
    assert config.secrets_dir == chosen.resolve() and chosen.resolve() in config.deny_read
    (home_b / "config.toml").write_text('[paths]\nsecret = "x"\n')
    with pytest.raises(ConfigError, match=r"\[paths\] に知らないキー"):
        load_config(env={"KEI_AGENT_HOME": str(home_b)})


def test_profile_is_added_to_conversation_prompts_but_not_to_json_only_ones(tmp_path):
    """話し方や所属はプロフィールから。振り分け・選別のように JSON だけを返す係には足さない。"""
    home = _home(tmp_path, profile="# プロフィール\n\n<!-- 書き方の説明 -->\n## 話し方\n\n- 一人称は「僕」\n")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    course = prompt_text(config, config.prompt_file("course.md"))
    assert course.rstrip().endswith("## 依頼者のプロフィール\n\n## 話し方\n\n- 一人称は「僕」")
    assert "# プロフィール" not in course and "書き方の説明" not in course     # 題とコメントは差し込まない
    assert "依頼者のプロフィール" not in prompt_text(config, config.prompt_file("router.md"))
    assert "依頼者のプロフィール" not in prompt_text(config, config.prompt_file("knowledge-digest.md"))
    # プロフィールを変えたら、指示の版も変わる（古い会話を、前の話し方のまま続けない）
    before = prompt_version(config, "course")
    (home / "profile.md").write_text("## 話し方\n\n- 一人称は「私」\n", encoding="utf-8")
    assert prompt_version(config, "course") != before


def test_a_prompt_can_be_replaced_as_a_whole(tmp_path):
    home = _home(tmp_path, prompts={"course.md": "# 自分の大学エージェント\n"})
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.prompt_file("course.md") == home.resolve() / "prompts" / "course.md"
    assert config.prompt_file("work.md") == config.repo_root / "prompts" / "work.md"
    assert prompt_text(config, config.prompt_file("course.md")) == "# 自分の大学エージェント\n"


def test_the_example_profile_adds_only_its_content(tmp_path):
    """例のプロフィールをそのまま写しても、書き方の説明は指示書に入らない。"""
    from kei_agent.config import REPO_ROOT

    home = _home(tmp_path, profile=(REPO_ROOT / "profile.example.md").read_text(encoding="utf-8"))
    text = load_config(env={"KEI_AGENT_HOME": str(home)}).profile_text
    assert text.startswith("## 話し方") and "<!--" not in text and "写して書き換える" not in text


def test_launch_scripts_can_ask_where_the_secrets_are(tmp_path):
    """起動スクリプトは、本体を起動する前に秘密情報の置き場所を聞く。設定が読めなくても既定の場所を答える。"""
    home = _home(tmp_path, f'[paths]\nsecrets = "{tmp_path / "local"}"\n')

    def ask(env_home):
        return subprocess.run([sys.executable, "-m", "kei_agent.paths", "secrets"], capture_output=True, text=True,
                              check=True, env={"KEI_AGENT_HOME": str(env_home), "PATH": "/usr/bin:/bin"}).stdout.strip()

    assert ask(home) == str((tmp_path / "local").resolve())
    assert ask(tmp_path / "nowhere") == str((tmp_path / "nowhere").resolve() / "secrets")
