import pytest

from kei_agent import themes
from kei_agent.agent_policy import policy_of
from kei_agent.themes import ChannelKind


def test_resolve_kinds(config):
    assert themes.resolve(config, "00_kei-agent").kind is ChannelKind.IMPROVE
    assert themes.resolve(config, "00_kei-agent").cwd is None

    overview = themes.resolve(config, "research-overview")
    assert overview.kind is ChannelKind.OVERVIEW
    assert overview.cwd == config.overview_dir

    theme = themes.resolve(config, "vlm-counting")
    assert theme.kind is ChannelKind.THEME
    assert theme.cwd == config.research_root / "vlm-counting"


def test_resolve_accepts_japanese_channel_names(config):
    assert themes.resolve(config, "視覚言語モデル").cwd == config.research_root / "視覚言語モデル"


@pytest.mark.parametrize("name", ["..", "../etc", "a/b", ".hidden", "_overview", ""])
def test_resolve_rejects_unsafe_names(config, name):
    with pytest.raises(ValueError):
        themes.resolve(config, name)


def test_ensure_workspace_creates_template_once(config):
    ws = themes.resolve(config, "vlm")
    assert themes.ensure_workspace(ws) is True
    for sub in themes.THEME_SUBDIRS:
        assert (ws.cwd / sub).is_dir()
    claude_md = ws.cwd / "CLAUDE.md"
    assert "# テーマ: vlm" in claude_md.read_text() and "#vlm" in claude_md.read_text()

    claude_md.write_text("編集済み")
    assert themes.ensure_workspace(ws) is False
    assert claude_md.read_text() == "編集済み"


def test_ensure_workspace_overview(config):
    ws = themes.resolve(config, "research-strategy")
    themes.ensure_workspace(ws)
    assert (ws.cwd / "CLAUDE.md").exists()
    assert (ws.cwd / "outputs").is_dir()


def test_unknown_config_key_is_reported(tmp_path):
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[schedule]\ndayly = "08:00"\n')
    with pytest.raises(ConfigError, match="dayly"):
        load_config(path, env={})


def test_unknown_key_in_channels_is_reported(tmp_path):
    """昔の theme_prefix が残っていても黙って無視しない（テーマのディレクトリが二重にできる）。"""
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[channels]\ntheme_prefix = "theme-"\n')
    with pytest.raises(ConfigError, match="theme_prefix"):
        load_config(path, env={})


def test_unknown_key_in_sandbox_is_reported(tmp_path):
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[sandbox]\nallowed_domain = ["example.com"]\n')
    with pytest.raises(ConfigError, match="allowed_domain"):
        load_config(path, env={})


def test_unknown_top_level_key_is_reported(tmp_path):
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('reserch_root = "~/research"\n')
    with pytest.raises(ConfigError, match="reserch_root"):
        load_config(path, env={})


def test_a_broken_schedule_time_is_reported(tmp_path):
    """`8:00` のような書き方は時刻として読めない。黙って「行わない」にせず、起動のときに断る。"""
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[schedule]\ndaily = "8:00"\n')
    with pytest.raises(ConfigError, match=r"\[schedule\] daily"):
        load_config(path, env={})

    path.write_text('[maintenance]\ntime = "22時"\n')
    with pytest.raises(ConfigError, match=r"\[maintenance\] time"):
        load_config(path, env={})

    # 空文字は「行わない」の意味なので通す
    path.write_text('[schedule]\nreview = ""\n')
    assert load_config(path, env={}).schedule.review == ""


def test_example_config_in_repo_loads_without_personal_values(tmp_path):
    """リポジトリには例の設定だけを置く。検査を通り、Notion のページ ID などの個人の値は空のまま。"""
    from kei_agent.config import EXAMPLE_CONFIG, REPO_ROOT, load_config
    assert not (REPO_ROOT / "config.toml").is_file() or (REPO_ROOT / ".gitignore").read_text().count("/config.toml")
    config = load_config(EXAMPLE_CONFIG, env={})
    assert (config.notion.hub_home, config.notion.research_home, config.notion.course_home) == ("", "", "")


def test_agent_profile_selects_codex_and_keeps_other_actors_unselected(tmp_path):
    from kei_agent.config import load_config
    path = tmp_path / "config.toml"
    path.write_text('[agents.research]\nprovider = "codex"\n')
    config = load_config(path, env={"KEI_AGENT_CODEX_BIN": "codex-test"})
    assert config.codex_bin == "codex-test"
    assert config.agent_profiles["research"].provider == "codex"
    assert set(config.agent_profiles["research"].__dataclass_fields__) == {"provider"}
    assert config.agent_profiles["course"].provider == ""


def test_connectors_are_decided_by_the_policy_table_not_the_config(tmp_path):
    """連携を config.toml でも宣言できると、制限の正本が2つになる。"""
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[agents.research]\nprovider = "codex"\nconnectors = ["wandb"]\n')

    with pytest.raises(ConfigError, match="connectors"):
        load_config(path, env={})


def test_old_model_recipe_configuration_is_rejected(tmp_path):
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text(
        '[model_recipes.routine]\nprovider = "codex"\nmodel = "gpt-routine"\nreasoning_effort = "low"\n'
        '[agents.research]\nprovider = "codex"\ndefault_recipe = "routine"\n'
    )
    with pytest.raises(ConfigError, match="model_recipes"):
        load_config(path, env={})


@pytest.mark.parametrize("connectors", ['"wandb"', '["wandb", "wandb"]'])
def test_agent_profile_rejects_invalid_connector_declarations(tmp_path, connectors):
    """非配列や重複を黙って許可して、意図しない connector を有効にする変更を捕捉する。"""
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text(f'[agents.research]\nconnectors = {connectors}\n')

    with pytest.raises(ConfigError, match="connectors"):
        load_config(path, env={})


def test_course_policy_reads_box_and_uses_notion_only_through_the_gateway():
    policy = policy_of("course")
    assert [app.name for app in policy.codex_apps] == ["Box"]
    assert policy.notion == "write"


def test_agent_workspaces_are_stable_places_for_sessions(config):
    course = themes.agent_workspace(config, "course")
    work = themes.agent_workspace(config, "work")
    assert (course.kind, course.cwd) == (ChannelKind.COURSE, config.course_root)
    assert work.kind is ChannelKind.WORK and work.cwd == config.state_dir / "agents" / "work"
    assert work.cwd.is_dir() and (course.cwd / "CLAUDE.md").exists()
    with pytest.raises(ValueError):
        themes.agent_workspace(config, "research")


# チャンネル名の先頭の番号（並び順のためのもの）


def test_theme_name_drops_the_sorting_number():
    assert themes.theme_name("10_amr-query") == "amr-query"
    assert themes.theme_name("00_kei-agent") == "kei-agent"
    assert themes.theme_name("amr-query") == "amr-query"       # 番号なしはそのまま
    assert themes.theme_name("2026_survey") == "survey"        # 4桁でも番号として外す
    assert themes.theme_name("a10_x") == "a10_x"               # 先頭が数字でなければ名前の一部


def test_numbered_channel_uses_the_same_directory(config):
    plain = themes.resolve(config, "amr-query")
    numbered = themes.resolve(config, "10_amr-query")
    assert numbered.cwd == plain.cwd and numbered.channel_name == "amr-query"


def test_course_channel_points_at_the_course_workspace(config):
    """大学のチャンネルの作業場は ~/course（本体はここで claude を動かさず、大学エージェントが使う）。"""
    ws = themes.resolve(config, "20_course")
    assert ws.kind is themes.ChannelKind.COURSE and ws.cwd == config.course_root
    assert themes.ensure_workspace(ws) is True
    assert "授業と課題の資料は Box" in (ws.cwd / "CLAUDE.md").read_text()


def test_knowledge_channel_is_its_own_kind_and_actor(config):
    ws = themes.resolve(config, "40_knowledge")
    assert ws.kind is ChannelKind.KNOWLEDGE and ws.cwd is None
    assert [themes.actor_of(kind) for kind in (ChannelKind.KNOWLEDGE, ChannelKind.COURSE, ChannelKind.WORK,
                                               ChannelKind.IMPROVE, ChannelKind.THEME, ChannelKind.OVERVIEW)] == [
        "knowledge", "course", "work", "self_fix", "research", "research"]
    assert themes.agent_workspace(config, "knowledge").cwd == config.state_dir / "agents" / "knowledge"
    # 研究テーマのディレクトリには、もう papers/ を作らない（論文は研究ホームの先行研究 DB）
    assert "papers" not in themes.THEME_SUBDIRS
