import re
import unicodedata
from pathlib import Path

from kei_agent.framework import modules

DOCS = ("README.md", "CONTRIBUTING.md", "deploy/README.md", "docs/using.md", "docs/architecture.md", "docs/modules.md",
        "docs/extensibility.md", "docs/agents.md", *(str(p) for p in sorted(Path("docs/agents").glob("*.md"))))


def test_architecture_documents_actor_scoped_read_only_execution_without_legacy_voice_codex():
    text = Path("docs/architecture.md").read_text(encoding="utf-8")

    assert "gpt-5.6" not in text
    assert "actor" in text
    assert "read-only" in text
    assert "固定 Codex" not in text


def test_every_user_facing_agent_prompt_requires_only_a_final_region():
    for path in ("modules/research/research.md", "modules/course/course.md", "modules/work/work.md", "modules/improve/improve.md",
                 "modules/daily/daily.md"):
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


def test_the_readme_lists_every_builtin_module():
    """README の組み込みのモジュールの表は、modules/ と同じ（足したり消したりしたら、表も直す）。"""
    rows = re.findall(r"^\| ([^|]+?) \| `([a-z][a-z0-9-]*)` \| [^|]+ \|$", Path("README.md").read_text(encoding="utf-8"),
                      re.MULTILINE)
    listed = {name: label for label, name in rows}
    assert len(rows) == len(listed), "README の表に同じモジュールが2回ある"
    assert listed == {name: spec.label for name, spec in modules.builtin().items()}, \
        "README の「何ができるか」の表を、modules/ のモジュール（名前と module.toml の label）に合わせてください"


def _slug(heading: str) -> str:
    """GitHub が見出しに付ける飛び先の名前（小文字にし、記号を外し、空白を - にする）。"""
    return "".join("-" if ch.isspace() else ch for ch in heading.strip().lower().replace("`", "")
                   if ch in "-_" or ch.isspace() or unicodedata.category(ch)[0] not in "PS")


def test_links_to_headings_point_to_existing_headings():
    heading = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)
    link = re.compile(r"\]\(([^)#\s]*)#([^)]+)\)")
    for doc in DOCS:
        path = Path(doc)
        for target, fragment in link.findall(path.read_text(encoding="utf-8")):
            linked = (path.parent / target) if target else path
            if linked.suffix != ".md":
                continue
            names = {_slug(h) for h in heading.findall(linked.read_text(encoding="utf-8"))}
            assert fragment in names, f"{doc}: {target}#{fragment} の見出しがない"
