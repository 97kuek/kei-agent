from pathlib import Path


def test_design_documents_actor_scoped_read_only_execution_without_legacy_voice_codex():
    text = Path("docs/design.md").read_text(encoding="utf-8")

    assert "gpt-5.6" not in text
    assert "actor" in text
    assert "read-only" in text
    assert "固定 Codex" not in text
