import pytest

from ezra import themes
from ezra.themes import ChannelKind


def test_resolve_kinds(config):
    assert themes.resolve(config, "assistant-improve").kind is ChannelKind.IMPROVE
    assert themes.resolve(config, "assistant-improve").cwd is None

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
    assert "#vlm" in claude_md.read_text()

    claude_md.write_text("編集済み")
    assert themes.ensure_workspace(ws) is False
    assert claude_md.read_text() == "編集済み"


def test_ensure_workspace_overview(config):
    ws = themes.resolve(config, "research-strategy")
    themes.ensure_workspace(ws)
    assert (ws.cwd / "CLAUDE.md").exists()
    assert (ws.cwd / "outputs").is_dir()
