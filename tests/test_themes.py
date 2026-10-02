from dataclasses import replace

import pytest
from fakes import write_agents, write_config

from kei_agent.workspaces import themes
from kei_agent.workspaces.themes import ChannelKind


def test_resolve_kinds(config):
    improve = themes.resolve(config, "0-kei-agent")
    assert improve.kind is ChannelKind.IMPROVE and improve.cwd is None

    overview = themes.resolve(config, "research-overview")
    assert overview.kind is ChannelKind.OVERVIEW and overview.cwd == config.overview_dir

    theme = themes.resolve(config, "vlm-counting")
    assert theme.kind is ChannelKind.THEME and theme.cwd == config.research_root / "vlm-counting"
    assert themes.resolve(config, "視覚言語モデル").cwd == config.research_root / "視覚言語モデル"
    # 番号つきのチャンネルも、番号なしと同じ作業場
    numbered = themes.resolve(config, "1-amr-query")
    assert numbered.cwd == themes.resolve(config, "amr-query").cwd and numbered.channel_name == "amr-query"


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

    overview = themes.resolve(config, "research-strategy")
    themes.ensure_workspace(overview)
    assert (overview.cwd / "CLAUDE.md").exists() and (overview.cwd / "outputs").is_dir()


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


@pytest.mark.parametrize(("text", "match"), [
    ('[schedule]\ndayly = "08:00"\n', "dayly"),
    ('[sandbox]\nallowed_domain = ["example.com"]\n', "allowed_domain"),
    ('reserch_root = "~/research"\n', "reserch_root"),
    # `8:00` のような書き方は時刻として読めない。黙って「行わない」にせず、起動のときに断る
    ('[schedule]\ndaily = "8:00"\n', r"\[schedule\] daily"),
    ('[maintenance]\ntime = "22時"\n', r"\[maintenance\] time"),
    ('[model_recipes.routine]\nprovider = "codex"\nmodel = "gpt-routine"\n', "model_recipes"),
    # 連携は制限の表が決め、モジュール・チャンネル・AI は担当の表だけに書く。書く場所を示す
    ('[agents.research]\nprovider = "codex"\nconnectors = ["wandb"]\n', "agents.csv"),
    ('modules = ["research"]\n', "agents.csv"),
    ('[channels]\ntheme_prefix = "theme-"\n', "agents.csv"),
])
def test_wrong_config_is_refused_at_startup(tmp_path, text, match):
    from kei_agent.configuration.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=match):
        load_config(path, env={})


def test_schedules_csv_turns_schedules_off_and_agents_csv_takes_only_ai_columns(tmp_path):
    from kei_agent.configuration.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text("")
    (tmp_path / "schedules.csv").write_text("name,enabled,time\nreview,false,21:00\nreading,true,06:30\n"
                                            "maintenance,false,22:00\n")
    config = load_config(path, env={})
    assert (config.schedule.review, config.schedule.module_times["reading"]) == ("", "06:30")
    assert not config.maintenance.enabled and config.schedule.daily == "08:00"      # 書いていない処理は既定
    for rows, said in (("daily,true,8:00\n", "HH:MM"), ("dayly,true,08:00\n", "知らない処理"),
                       ("daily,true,\n", "time を書いて")):
        (tmp_path / "schedules.csv").write_text("name,enabled,time\n" + rows)
        with pytest.raises(ConfigError, match=said):
            load_config(path, env={})
    (tmp_path / "schedules.csv").unlink()
    path.write_text('[schedule]\ndaily = "07:00"\n')
    with pytest.raises(ConfigError, match="schedules.csv"):
        load_config(path, env={})
    # 表にも、AI の列のほかは書けない
    write_config(path, "")
    (tmp_path / "agents.csv").write_text("module,enabled,channels,engine,model,effort,connectors\nresearch,true,,codex,,,wandb\n")
    with pytest.raises(ConfigError, match="1行目"):
        load_config(path, env={})


def test_example_config_in_repo_loads_without_personal_values(tmp_path):
    """リポジトリには例の設定だけを置く。検査を通り、Notion のページ ID などの個人の値は空のまま。"""
    from kei_agent.configuration.config import EXAMPLE_CONFIG, REPO_ROOT, load_config
    assert not (REPO_ROOT / "config.toml").is_file() or (REPO_ROOT / ".gitignore").read_text().count("/config.toml")
    config = load_config(EXAMPLE_CONFIG, env={})
    assert (config.notion.hub_home, config.notion.research_home, config.notion.course_home) == ("", "", "")


def test_agent_profile_selects_codex_and_keeps_other_actors_unselected(tmp_path):
    from kei_agent.configuration.config import load_config
    path = write_config(tmp_path / "config.toml", "")
    write_agents(tmp_path, [{"module": "research", "engine": "codex"}])
    config = load_config(path, env={"KEI_AGENT_CODEX_BIN": "codex-test"})
    assert config.codex_bin == "codex-test"
    assert config.agent_profiles["research"].provider == "codex"
    assert set(config.agent_profiles["research"].__dataclass_fields__) == {"provider", "model", "effort", "claude_account", "codex_account", "claude_email",
                                                                         "engines"}
    assert config.agent_profiles["course"].provider == ""


def test_agent_workspaces_are_stable_places_for_sessions(config):
    """大学のチャンネルは大学のモジュールのもの。担当の作業場は ~/course（設定の course_root）で、
    はじめて使うときに、モジュールのフォルダのひな形（AGENTS.template.md）から AGENTS.md を作る。"""
    ws = themes.resolve(config, "2-course")
    assert (ws.kind, ws.module, ws.cwd) == (ChannelKind.MODULE, "course", None)
    course = themes.agent_workspace(config, "course")
    work = themes.agent_workspace(config, "work")
    assert (course.kind, course.module, course.cwd) == (ChannelKind.MODULE, "course", config.course_root)
    assert "授業と課題の資料は Box" in (course.cwd / "AGENTS.md").read_text()
    # 仕事はモジュール。作業場は前と同じ場所（会話の続きが切れない）
    assert (work.kind, work.module) == (ChannelKind.MODULE, "work") and work.cwd == config.state_dir / "agents" / "work"
    assert work.cwd.is_dir() and (course.cwd / "CLAUDE.md").exists()
    # 実行役を持たないモジュール（声）には、作業場が無い
    with pytest.raises(ValueError):
        themes.agent_workspace(config, "voice")


# チャンネル名の先頭の番号（並び順のためのもの）


def test_theme_name_drops_the_sorting_number():
    assert themes.theme_name("1-amr-query") == "amr-query"
    assert themes.theme_name("0-kei-agent") == "kei-agent"
    assert themes.theme_name("amr-query") == "amr-query"       # 番号なしはそのまま
    assert themes.theme_name("3-work-billing") == "work-billing"
    assert themes.theme_name("2026-plan") == "2026-plan"       # 番号は1〜2桁と - だけ。年などは名前の一部
    assert themes.theme_name("10_amr-query") == "10_amr-query"  # 前の形（10_）は、もう番号として外さない
    assert themes.theme_name("a1-x") == "a1-x"                 # 先頭が数字でなければ名前の一部


def test_module_channel_belongs_to_its_module(config):
    """モジュールのチャンネル（module.toml の [channels]）は、研究テーマではなく、そのモジュールのもの。"""
    ws = themes.resolve(config, "4-knowledge")
    assert (ws.kind, ws.module, ws.cwd) == (ChannelKind.MODULE, "knowledge", None)
    assert [themes.actor_of(themes.resolve(config, name)) for name in (
        "4-knowledge", "2-course", "3-work", "0-kei-agent", "vlm", "0-overview")] == [
        "knowledge", "course", "work", "improve", "research", "research"]
    assert themes.agent_workspace(config, "knowledge").cwd == config.state_dir / "agents" / "knowledge"
    # 設定の [channels] で名前を変えたら、その名前がモジュールのもの。モジュールを外せば、ただのテーマ
    renamed = replace(config, module_channels={"knowledge": ("reading",)})
    assert themes.resolve(renamed, "reading").module == "knowledge"
    assert themes.resolve(renamed, "knowledge").kind is ChannelKind.THEME
    assert themes.resolve(replace(config, modules=(), module_channels={}), "knowledge").kind is ChannelKind.THEME
    # 研究テーマのディレクトリには、もう papers/ を作らない（論文は研究ホームの先行研究 DB）
    assert "papers" not in themes.THEME_SUBDIRS
