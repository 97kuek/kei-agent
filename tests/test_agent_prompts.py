from pathlib import Path


def test_connector_agents_share_kei_agents_relaxed_slack_voice():
    """Codex App経由でも、大学・仕事の返答を事務的な定型文に戻さない。"""
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    for name in ("course.md", "work.md"):
        text = (prompts / name).read_text(encoding="utf-8")
        # 口調は利用者のプロフィールの「話し方」に従う（指示書に個人の口調を書かない）
        assert "依頼者のプロフィール」の「話し方」に従う" in text and "僕" not in text
        assert "事務的な言い方や作業の実況から始めない" in text
