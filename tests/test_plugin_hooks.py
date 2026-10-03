"""plugin に同梱した PreToolUse の hook。

`--allowedTools` / `--disallowedTools` が第一防御で、ここは第二防御。
はっきりした安全違反（担当外のサービスへの書き込み、生トークンの持ち出し）だけを断る。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

import pytest

from kei_agent.configuration.config import REPO_ROOT
from kei_agent.execution.agent_policy import NOTION_READ_TOOLS, policy_of

# 担当ごとの skill とフックの置き場所。本体の担当は plugin/<名前>、モジュールの担当はそのフォルダの plugin/
PLUGINS = {"research": REPO_ROOT / "modules" / "research" / "plugin",
           "work": REPO_ROOT / "modules" / "work" / "plugin"}
AGENTS = ("research", "work")
ALLOW, DENY = 0, 2


def run_policy(agent: str, event: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PLUGINS[agent] / "hooks" / "policy.py")],
        input=json.dumps(event), capture_output=True, text=True, check=False)


@pytest.mark.parametrize("agent", AGENTS)
def test_each_plugin_ships_its_hook(agent):
    hooks = json.loads((PLUGINS[agent] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for entry in hooks["hooks"]["PreToolUse"] for h in entry["hooks"]]

    assert any("${CLAUDE_PLUGIN_ROOT}/hooks/policy.py" in c for c in commands)
    policy = PLUGINS[agent] / "hooks" / "policy.py"
    assert policy.stat().st_mode & 0o111
    # hook は plugin の中だけで動く（Kei Agent のパッケージを import しない）
    assert "kei_agent" not in policy.read_text(encoding="utf-8")


@pytest.mark.parametrize(("agent", "tool", "allowed"), [
    # 大学: Box は読むだけ、Notion はゲートウェイ（授業ホームの中）なら全部
    # 仕事: 読むだけ
    ("work", "mcp__claude_ai_Microsoft_365__outlook_email_search", True),
    ("work", "mcp__claude_ai_Microsoft_365__read_resource", True),
    ("work", "mcp__claude_ai_Microsoft_365__outlook_send_mail", False),
    ("work", "mcp__claude_ai_Microsoft_365__teams_send_channel_message", False),
    ("work", "mcp__claude_ai_Microsoft_365__outlook_create_event", False),
    # 仕事: Teams・SharePoint も読むだけ
    ("work", "mcp__claude_ai_Microsoft_365__chat_message_search", True),
    ("work", "mcp__claude_ai_Microsoft_365__teams_list_channel_messages", True),
    ("work", "mcp__claude_ai_Microsoft_365__sharepoint_search", True),
    ("work", "mcp__claude_ai_Microsoft_365__teams_reply_channel_message", False),
    ("work", "mcp__claude_ai_Microsoft_365__teams_create_chat", False),
    ("work", "mcp__claude_ai_Microsoft_365__sharepoint_delete_item", False),
    ("work", "mcp__claude_ai_Microsoft_365__sharepoint_move_item", False),
    # 研究: Notion はゲートウェイだけ
    ("research", "mcp__kei-notion__create_page", True),
    ("research", "mcp__kei-notion__delete_block", True),
    ("research", "mcp__claude_ai_Notion__notion-search", False),
    ("research", "mcp__claude_ai_Notion__notion-update-page", False),
    ("research", "Read", True),
    # 研究: 名前に notion を含む server はゲートウェイのほか全部断る（大文字や別名も）
    ("research", "mcp__work-notion__notion-search", False),
    ("research", "mcp__NOTION__search", False),
    ("research", "mcp__research-notion-evil__search", False),
    ("research", "mcp__kei-notion-evil__search", False),
    ("research", "mcp__research-notion__search", False),
    # 担当外の連携は、読むだけでも使わない
    ("work", "mcp__claude_ai_Notion__notion-update-page", False),
    ("work", "mcp__claude_ai_Notion__notion-search", False),
    ("work", "mcp__claude_ai_Box__upload_file", False),
    ("work", "mcp__claude_ai_Slack__slack_search_public", False),
    # Codex の名前。MCP の server の `-` は `_` になり、App の道具はフックに mcp__codex_apps__<App>__<道具> で届く
    # （モデルには mcp__codex_apps__<App>_<道具> で見える。どちらでも同じに判定する）
    ("work", "mcp__codex_apps__microsoft_outlook_email__search_messages", True),
    ("work", "mcp__codex_apps__microsoft_outlook_calendar__get_schedule", True),
    ("work", "mcp__codex_apps__microsoft_outlook_email__send_email", False),
    ("work", "mcp__codex_apps__microsoft_outlook_email_send_email", False),
    ("work", "mcp__codex_apps__microsoft_outlook_email__draft_email", False),
    ("work", "mcp__codex_apps__microsoft_outlook_email__mark_email_read_state", False),
    ("work", "mcp__codex_apps__microsoft_outlook_email__schedule_email", False),
    ("work", "mcp__codex_apps__microsoft_outlook_calendar__cancel_or_delete_event", False),
    ("work", "mcp__codex_apps__microsoft_outlook_calendar__add_event_attachment", False),
    ("work", "mcp__codex_apps__slack__search", False),
    ("research", "mcp__kei_notion__create_page", True),
    ("research", "mcp__kei_notion_evil__search", False),
    ("research", "mcp__codex_apps__notion__notion-search", False),
])
def test_hook_policy(agent, tool, allowed):
    result = run_policy(agent, {"tool_name": tool, "tool_input": {}})

    assert (result.returncode == ALLOW) is allowed, result.stderr


def _decide(agent: str):
    spec = importlib.util.spec_from_file_location(f"{agent}_policy", PLUGINS[agent] / "hooks" / "policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.decide


@pytest.mark.parametrize("agent", AGENTS)
def test_every_tool_the_policy_table_allows_passes_the_hook(agent):
    """一の柵（制限の表）で渡す道具は、Claude と Codex のどちらの名前でも、二の柵で断られない。"""
    policy = policy_of(agent)
    names = [name for connector in policy.connectors for name in connector.claude_names()]
    names += ["mcp__codex_apps__" + tool.replace(".", sep) for app in policy.codex_apps for tool in app.tools
              for sep in ("__", "_")]
    if policy.notion != "none":
        names += [f"mcp__{server}__{tool}" for server in ("kei-notion", "kei_notion")
                  for tool in (*NOTION_READ_TOOLS, "create_page", "move")]
    decide = _decide(agent)
    refused = [name for name in names if decide({"tool_name": name, "tool_input": {}})[0] != ALLOW]
    assert refused == []


@pytest.mark.parametrize(("command", "expected"), [
    ("echo $NOTION_TOKEN", DENY),
    ("env | grep NOTION_TOKEN", DENY),
    ('curl -H "Authorization: Bearer $NOTION_TOKEN" https://api.notion.com/v1/search', DENY),
    ("echo $NOTION_PERSONAL_TOKEN", DENY),
    ("printenv | grep MY_NOTION_API_TOKEN", DENY),
    # 親の合言葉があれば、どのホームの合言葉も作れてしまう。研究 claude には渡していない
    ('test -n "$KEI_AGENT_NOTION_GATEWAY_TOKEN"', DENY),
    # 研究用の合言葉（研究ホームにしか届かない）の名前や、ふつうの Bash は止めない
    ("python3 train.py --epochs 3", ALLOW),
    ('test -n "$KEI_AGENT_NOTION_GATEWAY_AUTH"', ALLOW),
])
def test_research_hook_refuses_reaching_for_raw_notion_tokens(command, expected):
    result = run_policy("research", {"tool_name": "Bash", "tool_input": {"command": command}})

    assert result.returncode == expected


@pytest.mark.parametrize("agent", AGENTS)
def test_unreadable_input_is_refused(agent):
    broken = subprocess.run([sys.executable, str(PLUGINS[agent] / "hooks" / "policy.py")],
                            input="これは JSON ではない", capture_output=True, text=True, check=False)

    assert broken.returncode == DENY


@pytest.mark.parametrize(("agent", "tool", "expected"), [
    # 道具の名前が読めても中身が壊れているとき。書き込み系は断り、読み取り系は allowlist に任せる
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
