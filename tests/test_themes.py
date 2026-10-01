from dataclasses import replace

import pytest
from fakes import write_config

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
    notes = ws.cwd / "AGENTS.md"
    assert "# テーマ: vlm" in notes.read_text() and "#vlm" in notes.read_text()
    assert (ws.cwd / "CLAUDE.md").read_text() == "@AGENTS.md\n"     # Claude Code は AGENTS.md を読み込む

    notes.write_text("編集済み")
    assert themes.ensure_workspace(ws) is False
    assert notes.read_text() == "編集済み"


def test_an_old_claude_md_moves_to_agents_md_but_not_in_someone_elses_folder(config, tmp_path):
    """Kei Agent が作った作業場の CLAUDE.md は AGENTS.md に移す（Codex も読めるように）。既存のフォルダのものは動かさない。"""
    ws = themes.resolve(config, "vlm")
    ws.cwd.mkdir(parents=True)
    (ws.cwd / "CLAUDE.md").write_text("# 前からの前提\n")
    themes.ensure_workspace(ws)
    assert (ws.cwd / "AGENTS.md").read_text() == "# 前からの前提\n"
    assert (ws.cwd / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert themes.notes_file(ws.cwd).name == "AGENTS.md"

    theirs = replace(ws, cwd=tmp_path / "theirs", external=True)
    theirs.cwd.mkdir()
    (theirs.cwd / "CLAUDE.md").write_text("# その人の前提\n")
    themes.ensure_workspace(theirs)
    assert not (theirs.cwd / "AGENTS.md").exists() and themes.notes_file(theirs.cwd).name == "CLAUDE.md"


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
    path = write_config(tmp_path / "config.toml", '[agents.research]\nprovider = "codex"\n')
    config = load_config(path, env={"KEI_AGENT_CODEX_BIN": "codex-test"})
    assert config.codex_bin == "codex-test"
    assert config.agent_profiles["research"].provider == "codex"
    assert set(config.agent_profiles["research"].__dataclass_fields__) == {"provider", "model", "effort"}
    assert config.agent_profiles["course"].provider == ""


def test_old_model_recipe_configuration_is_rejected(tmp_path):
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text(
        '[model_recipes.routine]\nprovider = "codex"\nmodel = "gpt-routine"\nreasoning_effort = "low"\n')
    with pytest.raises(ConfigError, match="model_recipes"):
        load_config(path, env={})


def test_connectors_and_modules_in_config_toml_are_refused_with_how_to_move(tmp_path):
    """連携は制限の表が決め、モジュール・チャンネル・AI は担当の表だけに書く。config.toml に残っていれば、移し方を示す。"""
    from kei_agent.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    for text in ('[agents.research]\nprovider = "codex"\nconnectors = ["wandb"]\n', 'modules = ["research"]\n',
                 '[channels]\ntheme_prefix = "theme-"\n'):
        path.write_text(text)
        with pytest.raises(ConfigError, match="kei-agent agents init"):
            load_config(path, env={})
    # 表にも、AI の列のほかは書けない
    write_config(path, "")
    (tmp_path / "agents.csv").write_text("module,enabled,channels,engine,model,effort,connectors\nresearch,true,,codex,,,wandb\n")
    with pytest.raises(ConfigError, match="1行目"):
        load_config(path, env={})


def test_course_policy_reads_box_and_uses_notion_only_through_the_gateway():
    policy = policy_of("course")
    assert [app.name for app in policy.codex_apps] == ["Box"]
    assert policy.notion == "write"


def test_agent_workspaces_are_stable_places_for_sessions(config):
    course = themes.agent_workspace(config, "course")
    work = themes.agent_workspace(config, "work")
    assert (course.kind, course.module, course.cwd) == (ChannelKind.MODULE, "course", config.course_root)
    # 仕事はモジュール。作業場は前と同じ場所（会話の続きが切れない）
    assert (work.kind, work.module) == (ChannelKind.MODULE, "work") and work.cwd == config.state_dir / "agents" / "work"
    assert work.cwd.is_dir() and (course.cwd / "CLAUDE.md").exists()
    # 実行役を持たないモジュール（声）には、作業場が無い
    with pytest.raises(ValueError):
        themes.agent_workspace(config, "voice")


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
    """大学のチャンネルは大学のモジュールのもの。担当の作業場は ~/course（設定の course_root）で、
    はじめて使うときに、モジュールのフォルダのひな形（AGENTS.template.md）から AGENTS.md を作る。"""
    ws = themes.resolve(config, "20_course")
    assert (ws.kind, ws.module, ws.cwd) == (themes.ChannelKind.MODULE, "course", None)
    agent = themes.agent_workspace(config, "course")
    assert agent.cwd == config.course_root
    assert "授業と課題の資料は Box" in (agent.cwd / "AGENTS.md").read_text()


def test_module_channel_belongs_to_its_module(config):
    """モジュールのチャンネル（module.toml の [channels]）は、研究テーマではなく、そのモジュールのもの。"""
    ws = themes.resolve(config, "40_knowledge")
    assert (ws.kind, ws.module, ws.cwd) == (ChannelKind.MODULE, "knowledge", None)
    assert [themes.actor_of(themes.resolve(config, name)) for name in (
        "40_knowledge", "20_course", "30_work", "00_kei-agent", "vlm", "01_overview")] == [
        "knowledge", "course", "work", "improve", "research", "research"]
    assert themes.agent_workspace(config, "knowledge").cwd == config.state_dir / "agents" / "knowledge"
    # 設定の [channels] で名前を変えたら、その名前がモジュールのもの。モジュールを外せば、ただのテーマ
    renamed = replace(config, module_channels={"knowledge": ("reading",)})
    assert themes.resolve(renamed, "reading").module == "knowledge"
    assert themes.resolve(renamed, "knowledge").kind is ChannelKind.THEME
    assert themes.resolve(replace(config, modules=(), module_channels={}), "knowledge").kind is ChannelKind.THEME
    # 研究テーマのディレクトリには、もう papers/ を作らない（論文は研究ホームの先行研究 DB）
    assert "papers" not in themes.THEME_SUBDIRS
