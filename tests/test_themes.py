import pytest

from ezra import themes
from ezra.themes import ChannelKind


def test_resolve_kinds(config):
    assert themes.resolve(config, "research-ezra").kind is ChannelKind.IMPROVE
    assert themes.resolve(config, "research-ezra").cwd is None

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
    from ezra.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[schedule]\ndayly = "08:00"\n')
    with pytest.raises(ConfigError, match="dayly"):
        load_config(path, env={})


def test_unknown_key_in_channels_is_reported(tmp_path):
    """昔の theme_prefix が残っていても黙って無視しない（テーマのディレクトリが二重にできる）。"""
    from ezra.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[channels]\ntheme_prefix = "theme-"\n')
    with pytest.raises(ConfigError, match="theme_prefix"):
        load_config(path, env={})


def test_unknown_key_in_sandbox_is_reported(tmp_path):
    from ezra.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('[sandbox]\nallowed_domain = ["example.com"]\n')
    with pytest.raises(ConfigError, match="allowed_domain"):
        load_config(path, env={})


def test_unknown_top_level_key_is_reported(tmp_path):
    from ezra.config import ConfigError, load_config
    path = tmp_path / "config.toml"
    path.write_text('reserch_root = "~/research"\n')
    with pytest.raises(ConfigError, match="reserch_root"):
        load_config(path, env={})


def test_config_toml_in_repo_loads(tmp_path):
    """リポジトリの config.toml が、検査を通ること。"""
    from ezra.config import REPO_ROOT, load_config
    load_config(REPO_ROOT / "config.toml", env={})
