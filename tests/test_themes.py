import pytest

from ezra import themes
from ezra.themes import ChannelKind


def test_resolve_kinds(config):
    assert themes.resolve(config, "research-ezra").kind is ChannelKind.IMPROVE
    assert themes.resolve(config, "research-ezra").cwd is None

    overview = themes.resolve(config, "research-overview")
    assert overview.kind is ChannelKind.OVERVIEW
    assert overview.cwd == config.research_root / "_overview"

    theme = themes.resolve(config, "theme-vlm-counting")
    assert theme.kind is ChannelKind.THEME
    assert theme.cwd == config.research_root / "vlm-counting" and theme.theme == "vlm-counting"
    assert themes.theme_channel_name(config, "vlm-counting") == "theme-vlm-counting"


def test_channels_without_prefix_are_ignored(config):
    for name in ("inbox", "vlm", "ezra-trial"):
        ws = themes.resolve(config, name)
        assert ws.kind is ChannelKind.OTHER and ws.cwd is None
    assert themes.ensure_workspace(themes.resolve(config, "inbox")) is False
    assert not config.research_root.exists()


def test_resolve_accepts_japanese_channel_names(config):
    assert themes.resolve(config, "theme-視覚言語モデル").cwd == config.research_root / "視覚言語モデル"


@pytest.mark.parametrize("name", ["theme-..", "theme-../etc", "theme-a/b", "theme-.hidden", "theme-_overview", "theme-"])
def test_resolve_rejects_unsafe_names(config, name):
    with pytest.raises(ValueError):
        themes.resolve(config, name)


def test_ensure_workspace_creates_template_once(config):
    ws = themes.resolve(config, "theme-vlm")
    assert themes.ensure_workspace(ws) is True
    for sub in themes.THEME_SUBDIRS:
        assert (ws.cwd / sub).is_dir()
    claude_md = ws.cwd / "CLAUDE.md"
    assert "# テーマ: vlm" in claude_md.read_text() and "#theme-vlm" in claude_md.read_text()

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
