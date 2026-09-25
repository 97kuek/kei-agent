"""plugin に同梱した PreToolUse の hook。

`--allowedTools` / `--disallowedTools` が第一防御で、ここは第二防御。
はっきりした安全違反（担当外のサービスへの書き込み、生トークンの持ち出し）だけを断る。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from kei_agent.config import REPO_ROOT

PLUGIN = REPO_ROOT / "plugin"
AGENTS = ("research", "course", "work")
ALLOW, DENY = 0, 2


def run_policy(agent: str, event: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PLUGIN / agent / "hooks" / "policy.py")],
        input=json.dumps(event), capture_output=True, text=True, check=False)


@pytest.mark.parametrize("agent", AGENTS)
def test_each_plugin_ships_its_hook(agent):
    hooks = json.loads((PLUGIN / agent / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for entry in hooks["hooks"]["PreToolUse"] for h in entry["hooks"]]

    assert any("${CLAUDE_PLUGIN_ROOT}/hooks/policy.py" in c for c in commands)
    assert (PLUGIN / agent / "hooks" / "policy.py").stat().st_mode & 0o111


@pytest.mark.parametrize(("agent", "tool", "allowed"), [
    # 大学: Box は読むだけ、Notion は全部
    ("course", "mcp__claude_ai_Box__get_file_content", True),
    ("course", "mcp__claude_ai_Box__search_files_keyword", True),
    ("course", "mcp__claude_ai_Box__upload_file", False),
    ("course", "mcp__claude_ai_Box__create_folder", False),
    ("course", "mcp__claude_ai_Notion__notion-move-pages", True),
    ("course", "mcp__claude_ai_Notion__notion-create-database", True),
    # 仕事: 読むだけ
    ("work", "mcp__claude_ai_Microsoft_365__outlook_email_search", True),
    ("work", "mcp__claude_ai_Microsoft_365__read_resource", True),
    ("work", "mcp__claude_ai_Microsoft_365__outlook_send_mail", False),
    ("work", "mcp__claude_ai_Microsoft_365__teams_send_channel_message", False),
    ("work", "mcp__claude_ai_Microsoft_365__outlook_create_event", False),
    # 研究: Notion はゲートウェイだけ
    ("research", "mcp__research-notion__create_page", True),
    ("research", "mcp__research-notion__archive", True),
    ("research", "mcp__claude_ai_Notion__notion-search", False),
    ("research", "mcp__claude_ai_Notion__notion-update-page", False),
    ("research", "Read", True),
    # 研究: 名前に notion を含む server はゲートウェイのほか全部断る（大文字や別名も）
    ("research", "mcp__work-notion__notion-search", False),
    ("research", "mcp__NOTION__search", False),
    ("research", "mcp__research-notion-evil__search", False),
    # 担当外の連携への書き込みは断る。読むだけなら allowlist に任せる
    ("course", "mcp__claude_ai_Slack__slack_send_message", False),
    ("course", "mcp__claude_ai_Microsoft_365__outlook_create_draft", False),
    ("course", "mcp__claude_ai_Slack__slack_read_channel", True),
    ("work", "mcp__claude_ai_Notion__notion-update-page", False),
    ("work", "mcp__claude_ai_Box__upload_file", False),
    ("work", "mcp__claude_ai_Slack__slack_search_public", True),
])
def test_hook_policy(agent, tool, allowed):
    result = run_policy(agent, {"tool_name": tool, "tool_input": {}})

    assert (result.returncode == ALLOW) is allowed, result.stderr


@pytest.mark.parametrize("command", [
    "echo $NOTION_TOKEN",
    "env | grep NOTION_TOKEN",
    'curl -H "Authorization: Bearer $NOTION_TOKEN" https://api.notion.com/v1/search',
])
def test_research_hook_refuses_reaching_for_the_raw_notion_token(command):
    result = run_policy("research", {"tool_name": "Bash", "tool_input": {"command": command}})

    assert result.returncode == DENY


@pytest.mark.parametrize("command", [
    "echo $NOTION_COURSE_TOKEN",
    "printenv | grep MY_NOTION_API_TOKEN",
])
def test_research_hook_refuses_other_raw_notion_tokens(command):
    result = run_policy("research", {"tool_name": "Bash", "tool_input": {"command": command}})

    assert result.returncode == DENY


def test_research_hook_lets_the_gateway_token_name_through():
    assert run_policy("research", {
        "tool_name": "Bash",
        "tool_input": {"command": 'test -n "$KEI_AGENT_NOTION_GATEWAY_TOKEN"'}}).returncode == ALLOW


def test_research_hook_lets_the_gateway_token_through():
    """ゲートウェイの合言葉は研究 claude が持ってよいもの。ふつうの Bash も止めない。"""
    assert run_policy("research", {
        "tool_name": "Bash",
        "tool_input": {"command": "python3 train.py --epochs 3"}}).returncode == ALLOW


@pytest.mark.parametrize("agent", AGENTS)
def test_unreadable_input_is_refused(agent):
    broken = subprocess.run([sys.executable, str(PLUGIN / agent / "hooks" / "policy.py")],
                            input="これは JSON ではない", capture_output=True, text=True, check=False)

    assert broken.returncode == DENY


@pytest.mark.parametrize(("agent", "tool", "expected"), [
    # 道具の名前が読めても中身が壊れているとき。書き込み系は断り、読み取り系は allowlist に任せる
    ("course", "mcp__claude_ai_Box__upload_file", DENY),
    ("course", "mcp__claude_ai_Box__get_file_content", ALLOW),
    ("work", "mcp__claude_ai_Microsoft_365__outlook_send_mail", DENY),
    ("work", "mcp__claude_ai_Microsoft_365__outlook_email_search", ALLOW),
])
def test_a_broken_tool_input_falls_closed_only_for_writes(agent, tool, expected):
    result = run_policy(agent, {"tool_name": tool, "tool_input": "壊れている"})

    assert result.returncode == expected


@pytest.mark.parametrize("agent", AGENTS)
def test_the_hook_never_echoes_the_tool_input(agent):
    result = run_policy(agent, {
        "tool_name": "mcp__claude_ai_Box__upload_file",
        "tool_input": {"content": "do-not-log", "token": "ntn_do-not-log"}})

    assert "do-not-log" not in (result.stdout + result.stderr)


def test_every_policy_is_self_contained():
    """hook は plugin の中だけで動く（Kei Agent のパッケージを import しない）。"""
    for agent in AGENTS:
        body = (PLUGIN / agent / "hooks" / "policy.py").read_text(encoding="utf-8")
        assert "kei_agent" not in body, agent
