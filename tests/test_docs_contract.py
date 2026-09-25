import re
from pathlib import Path

DOCS = ("README.md", "CONTRIBUTING.md", "deploy/README.md", "docs/using.md", "docs/architecture.md")


def test_architecture_documents_actor_scoped_read_only_execution_without_legacy_voice_codex():
    text = Path("docs/architecture.md").read_text(encoding="utf-8")

    assert "gpt-5.6" not in text
    assert "actor" in text
    assert "read-only" in text
    assert "固定 Codex" not in text


def test_every_user_facing_agent_prompt_requires_only_a_final_region():
    for path in ("prompts/system.md", "prompts/course.md", "prompts/work.md"):
        text = Path(path).read_text(encoding="utf-8")
        assert "<<kei-agent-final>>" in text
        assert "<<kei-agent-final-end>>" in text
        assert "作業手順" in text and "Slack に出さない" in text


def test_relative_links_in_docs_point_to_existing_files():
    link = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
    for doc in DOCS:
        path = Path(doc)
        for target in link.findall(path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("mailto:"):
                continue
            assert (path.parent / target).exists(), f"{doc}: {target} がない"
