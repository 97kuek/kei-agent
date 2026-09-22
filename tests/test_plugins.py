"""エージェントごとの plugin（skill の置き場）の形。

担当外の plugin を同じ claude に読ませないので、plugin は「エージェント1つ＝ディレクトリ1つ」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kei_agent.config import REPO_ROOT

PLUGIN = REPO_ROOT / "plugin"
AGENTS = ("research", "course", "work")
# SKILL.md の本文の長さの上限（語数）。長いものは references/ に分ける
MAX_WORDS = 500


def frontmatter(path: Path) -> dict[str, str]:
    """SKILL.md の先頭の `---` に挟まれた `key: value` を読む。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if _ and not key.startswith(" "):
            found[key.strip()] = value.strip()
    return found


def skill_metadata(plugin_dir: Path) -> dict[str, dict[str, str]]:
    """その plugin が持つ skill の名前と frontmatter。"""
    return {path.parent.name: frontmatter(path) for path in sorted(plugin_dir.glob("skills/*/SKILL.md"))}


def test_research_plugin_has_renamed_skills():
    skills = skill_metadata(PLUGIN / "research")
    assert {"running-jobs", "researching-literature"} <= set(skills)
    assert not (PLUGIN / "skills").exists()
    assert not (PLUGIN / ".claude-plugin").exists()


@pytest.mark.parametrize("agent", AGENTS)
def test_each_agent_has_its_own_plugin_manifest(agent):
    manifest = json.loads((PLUGIN / agent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == f"kei-agent-{agent}"
    assert manifest["description"]


@pytest.mark.parametrize("agent", AGENTS)
def test_every_skill_says_its_name_and_when_to_use_it(agent):
    for name, meta in skill_metadata(PLUGIN / agent).items():
        assert meta.get("name") == name, f"{agent}/{name}: frontmatter の name がディレクトリ名と違う"
        assert meta.get("description", "").startswith("Use when"), f"{agent}/{name}: description は使用条件から書く"


@pytest.mark.parametrize("agent", AGENTS)
def test_skills_stay_short_enough_to_read(agent):
    for name, _ in skill_metadata(PLUGIN / agent).items():
        body = (PLUGIN / agent / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert len(body.split()) <= MAX_WORDS, f"{agent}/{name}: SKILL.md が長い（references/ に分ける）"


def test_skills_do_not_depend_on_an_environment_variable_for_their_scripts():
    """skill のスクリプトは plugin の中から辿る。環境変数が無いと動かない形にしない。"""
    for path in PLUGIN.glob("*/skills/*/SKILL.md"):
        assert "KEI_AGENT_PLUGIN_DIR" not in path.read_text(encoding="utf-8"), path


def test_course_plugin_has_three_scoped_skills():
    assert set(skill_metadata(PLUGIN / "course")) == {
        "finding-course-materials", "managing-assignments", "managing-course-notion", "managing-academic-record"}


def test_work_plugin_has_three_scoped_skills():
    assert set(skill_metadata(PLUGIN / "work")) == {
        "researching-work-context", "preparing-meetings", "drafting-work-actions"}


@pytest.mark.parametrize(("agent", "word"), [
    ("research", "Notion"),
    ("course", "Box"),
    ("work", "送"),
])
def test_each_plugin_says_what_it_must_not_touch(agent, word):
    """権限の境界は、どこか1つの skill には必ず書いてある。"""
    bodies = "\n".join((PLUGIN / agent / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
                       for name in skill_metadata(PLUGIN / agent))
    assert word in bodies
