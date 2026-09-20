import pytest

from kei_agent import themes
from kei_agent.themes import ChannelKind


def test_resolve_kinds(config):
    assert themes.resolve(config, "research-agent").kind is ChannelKind.IMPROVE
    assert themes.resolve(config, "research-agent").cwd is None

    overview = themes.resolve(config, "research-overview")
    assert overview.kind is ChannelKind.OVERVIEW
    assert overview.cwd == config.research_root / "_overview"

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


def test_config_toml_in_repo_loads(tmp_path):
    """リポジトリの config.toml が、検査を通ること。"""
    from kei_agent.config import REPO_ROOT, load_config
    load_config(REPO_ROOT / "config.toml", env={})


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
