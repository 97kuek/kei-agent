"""エージェントごとの plugin（skill の置き場）の形。

担当外の plugin を同じ claude に読ませないので、plugin は「エージェント1つ＝ディレクトリ1つ」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kei_agent.configuration.config import REPO_ROOT

PLUGIN = REPO_ROOT / "plugin"
# 担当ごとの skill とフックの置き場所。本体の担当は plugin/<名前>、モジュールの担当はそのフォルダの plugin/
PLUGINS = {"research": REPO_ROOT / "modules" / "research" / "plugin",
           "work": REPO_ROOT / "modules" / "work" / "plugin"}
AGENTS = ("research", "work")
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


@pytest.mark.parametrize(("agent", "skills", "boundary"), [
    ("research", {"running-jobs", "researching-literature"}, "Notion"),
    ("work", {"researching-work-context", "preparing-meetings", "drafting-work-actions"}, "送"),
])
def test_each_agent_has_its_own_plugin_with_scoped_skills(agent, skills, boundary):
    manifest = json.loads((PLUGINS[agent] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == f"kei-agent-{agent}" and manifest["description"]
    found = skill_metadata(PLUGINS[agent])
    assert skills <= set(found) if agent == "research" else skills == set(found)
    bodies = []
    for name, meta in found.items():
        assert meta.get("name") == name, f"{agent}/{name}: frontmatter の name がディレクトリ名と違う"
        assert meta.get("description", "").startswith("Use when"), f"{agent}/{name}: description は使用条件から書く"
        body = (PLUGINS[agent] / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert len(body.split()) <= MAX_WORDS, f"{agent}/{name}: SKILL.md が長い（references/ に分ける）"
        # skill のスクリプトは plugin の中から辿る。環境変数が無いと動かない形にしない
        assert "KEI_AGENT_PLUGIN_DIR" not in body, f"{agent}/{name}"
        bodies.append(body)
    # 権限の境界は、どこか1つの skill には必ず書いてある
    assert boundary in "\n".join(bodies)


def test_the_old_shared_plugin_folder_is_gone():
    assert not (PLUGIN / "skills").exists() and not (PLUGIN / ".claude-plugin").exists()
