from pathlib import Path


def test_connector_agents_share_kei_agents_relaxed_slack_voice():
    """Codex App経由でも、大学・仕事の返答を事務的な定型文に戻さない。"""
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    for name in ("course.md", "work.md"):
        text = (prompts / name).read_text(encoding="utf-8")
        assert "隣の席の助手" in text
        assert "〜したよ" in text
