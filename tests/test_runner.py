import asyncio
import json
import os
from dataclasses import replace

import pytest

from kei_agent import guard, runner, themes
from kei_agent.config import AgentProfile


def test_settings_limit_theme_to_its_directory(config):
    ws = themes.resolve(config, "vlm")
    settings = guard.build_settings(config, ws)
    allow = settings["permissions"]["allow"]
    assert f"Read(/{ws.cwd}/**)" in allow
    assert f"Edit(/{ws.cwd}/**)" in allow
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["network"]["allowedDomains"] == ["export.arxiv.org"]


def test_settings_add_domains_allowed_for_the_theme(config):
    """Slack で許可した接続先は、基本の接続先に足して使う。"""
    ws = replace(themes.resolve(config, "vlm"), allowed_domains=("zenodo.org",))
    domains = guard.build_settings(config, ws)["sandbox"]["network"]["allowedDomains"]
    assert domains == ["export.arxiv.org", "zenodo.org"]


def test_settings_overview_reads_all_themes_but_writes_only_overview(config):
    ws = themes.resolve(config, "research-overview")
    allow = guard.build_settings(config, ws)["permissions"]["allow"]
    assert f"Read(/{config.research_root}/**)" in allow
    assert f"Edit(/{config.overview_dir}/**)" in allow
    assert f"Edit(/{config.research_root}/**)" not in allow


def test_command_resumes_session_and_ignores_user_settings(config):
    ws = themes.resolve(config, "vlm")
    cmd = runner.build_command(config, ws, "sess-1")
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    json.loads(cmd[cmd.index("--settings") + 1])
    assert "--resume" not in runner.build_command(config, ws, None)


def test_codex_profile_builds_a_jsonl_workspace_write_command(config):
    ws = themes.resolve(config, "vlm")
    config = replace(
        config,
        codex_bin="codex-test",
        agent_profiles={"research": AgentProfile(provider="codex", model="gpt-5.3-codex", reasoning_effort="high")},
    )

    cmd = runner.build_command(config, ws, "thread-1")

    assert cmd[:4] == ["codex-test", "exec", "--json", "--sandbox"]
    assert "workspace-write" in cmd
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5.3-codex"
    assert "--config" in cmd and "model_reasoning_effort=high" in cmd
    assert "--plugin-dir" not in cmd
    assert cmd[-1] == "-"


def test_codex_install_links_only_research_skills_into_theme_workspace(config):
    """Codex をテーマ直下から起動しても、研究 plugin の skill だけを発見できる。"""
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)

    runner.install_codex_skills(config, ws)

    skills_dir = ws.cwd / ".agents" / "skills"
    wandb = skills_dir / "managing-wandb"
    assert wandb.is_symlink()
    assert wandb.resolve() == config.agent_plugin_dir("research") / "skills" / "managing-wandb"
    assert not (skills_dir / "managing-academic-record").exists()


def test_apply_codex_events_maps_thread_message_and_command_activity():
    result = runner.RunResult()
    assert runner.apply_codex_event(result, {"type": "thread.started", "thread_id": "t1"}) is None
    activity = runner.apply_codex_event(result, {
        "type": "item.started",
        "item": {"type": "command_execution", "command": "pytest -q"},
    })
    assert activity == "実行している: pytest -q"
    runner.apply_codex_event(result, {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "完了"},
    })
    runner.apply_codex_event(result, {"type": "turn.completed", "usage": {"total_cost_usd": 0.2}})
    assert (result.session_id, result.text, result.is_error) == ("t1", "完了", False)


def test_system_prompt_warns_that_replies_do_not_auto_continue(config):
    """「続ける」と言い切って実際には止まる、という矛盾を防ぐための一文。"""
    text = runner.system_prompt_text(config)
    assert "自動で" in text and "続き" in text and "止まる" in text


def test_env_strips_secrets_and_adds_thread(config):
    base = {
        "PATH": "/bin",
        "SLACK_BOT_TOKEN": "xoxb-secret",
        "SLACK_APP_TOKEN": "xapp-secret",
        "NOTION_TOKEN": "ntn_secret",
        "KEI_AGENT_ALLOWED_USER_ID": "UME",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "parent",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep",
        "OPENAI_API_KEY": "sk-secret",
        "CODEX_API_KEY": "codex-secret",
    }
    env = runner.build_env(config, base, "C1", "123.456")
    assert "SLACK_BOT_TOKEN" not in env and "SLACK_APP_TOKEN" not in env and "NOTION_TOKEN" not in env
    assert "KEI_AGENT_ALLOWED_USER_ID" not in env
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep"
    assert env["KEI_AGENT_CHANNEL"] == "C1" and env["KEI_AGENT_THREAD_TS"] == "123.456"
    assert "KEI_AGENT_PLUGIN_DIR" not in env


def test_apply_event_keeps_domains_claude_asked_for():
    """Bash の allowed_domains で広げようとした接続先は、sandbox では断られる。Kei Agent がボタンにできるよう覚えておく。"""
    result = runner.RunResult()
    runner.apply_event(result, {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "name": "Bash",
        "input": {"command": "curl -I https://huggingface.co/x", "description": "重みのサイズを見る",
                  "allowed_domains": ["huggingface.co", "cdn-lfs.huggingface.co"]}}]}})
    runner.apply_event(result, {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "name": "Bash", "input": {"command": "ls", "allowed_domains": ["huggingface.co"]}}]}})
    assert result.requested_domains == [("huggingface.co", "重みのサイズを見る"), ("cdn-lfs.huggingface.co", "重みのサイズを見る")]


def test_apply_events():
    result = runner.RunResult()
    assert runner.apply_event(result, {"type": "system", "subtype": "init", "session_id": "s1"}) is None
    activity = runner.apply_event(result, {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "調べます"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "一覧を見る"}},
        ]},
    })
    assert activity == "実行している: 一覧を見る"
    runner.apply_event(result, {
        "type": "result", "subtype": "success", "session_id": "s1", "result": "完了",
        "is_error": False, "total_cost_usd": 0.1, "duration_ms": 1000,
    })
    assert (result.session_id, result.text, result.is_error, result.cost_usd) == ("s1", "完了", False, 0.1)
    assert result.activities == ["実行している: 一覧を見る"]


def test_missing_session_detected():
    result = runner.RunResult()
    runner.apply_event(result, {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "errors": ["No conversation found with session ID: x"],
    })
    assert result.session_missing


def test_describe_tool_truncates():
    text = runner.describe_tool("WebSearch", {"query": "あ" * 200})
    assert text.startswith("Web で検索している: ") and text.endswith("…") and len(text) < 100


def test_settings_deny_reading_secret_locations(config):
    """sandbox は既定で PC 全体を読めるので、秘密情報の置き場所を塞いでおく。"""
    ws = themes.resolve(config, "vlm")
    filesystem = guard.build_settings(config, ws)["sandbox"]["filesystem"]
    assert filesystem["denyRead"] == [str(p) for p in config.deny_read]
    assert filesystem["allowWrite"] == [str(p) for p in config.allow_write]


def test_default_deny_read_covers_tokens_and_keys(tmp_path):
    from kei_agent.config import load_config
    paths = [str(p) for p in load_config(tmp_path / "none.toml", env={}).deny_read]
    assert any(p.endswith("/.ssh") for p in paths)
    assert any(p.endswith("/.aws") for p in paths)
    assert any(p.endswith("/.claude") for p in paths)
    assert any("zsh/local" in p for p in paths)


async def test_run_claude_returns_even_if_a_left_over_process_holds_the_output(config, tmp_path, monkeypatch):
    """claude が終わっても、Bash が残したプロセスが出力を握っていることがある。そこで固まらない。

    直す前は stderr の EOF を待ち続けて、スレッドの順番待ちと並行枠を握ったままになっていた。
    """
    fake = tmp_path / "fake-claude.sh"
    fake.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "sleep 60 &\n"
        "echo $! > left-over.pid\n"
        'printf \'{"type":"result","subtype":"success","session_id":"s1","result":"ok","is_error":false}\\n\'\n'
    )
    fake.chmod(0o755)
    monkeypatch.setattr(runner, "EXIT_GRACE_SECONDS", 0.5)
    config = replace(config, claude_bin=str(fake))
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)

    result = await asyncio.wait_for(runner.run_claude(config, ws, "hi", None, "C1", "1.1"), timeout=10)

    assert result.text == "ok" and not result.is_error and not result.timed_out
    pid = int((ws.cwd / "left-over.pid").read_text())
    for _ in range(50):  # 残ったプロセスも片づける
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"claude が残したプロセス {pid} が生きています")


async def test_run_codex_reads_jsonl_and_installs_research_skills(config, tmp_path):
    """Codex JSONLの応答・進捗と、テーマ作業場の研究skillを同時に扱える。"""
    fake = tmp_path / "fake-codex.sh"
    fake.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "printf '%s\\n' "
        "'{\"type\":\"thread.started\",\"thread_id\":\"thread-1\"}' "
        "'{\"type\":\"item.started\",\"item\":{\"type\":\"command_execution\",\"command\":\"pwd\"}}' "
        "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"完了\"}}' "
        "'{\"type\":\"turn.completed\"}'\n"
    )
    fake.chmod(0o755)
    config = replace(
        config,
        codex_bin=str(fake),
        agent_profiles={"research": AgentProfile(provider="codex")},
    )
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)
    seen_text: list[str] = []
    seen_activity: list[str] = []

    async def on_text(text: str) -> None:
        seen_text.append(text)

    async def on_activity(activity: str) -> None:
        seen_activity.append(activity)

    result = await runner.run_claude(
        config, ws, "調べて", None, "C1", "1.1",
        on_activity=on_activity,
        on_text=on_text,
    )

    assert (result.session_id, result.text, result.is_error) == ("thread-1", "完了", False)
    assert seen_text == ["完了"]
    assert seen_activity == ["実行している: pwd"]
    assert (ws.cwd / ".agents" / "skills" / "managing-wandb").is_symlink()


def test_apply_event_reads_the_usage_limit_and_when_it_resets():
    """claude -p は上限に達すると `Claude AI usage limit reached|<エポック秒>` を返す。"""
    result = runner.RunResult()
    runner.apply_event(result, {"type": "result", "is_error": True,
                                "result": "Claude AI usage limit reached|1789800000"})
    assert result.limit_reset_at == 1789800000


def test_apply_event_without_a_reset_time_still_counts_as_a_limit():
    result = runner.RunResult()
    runner.apply_event(result, {"type": "result", "is_error": True,
                                "errors": ["Claude AI usage limit reached"]})
    assert result.limit_reset_at == runner.UNKNOWN_LIMIT_RESET


def test_a_normal_error_is_not_a_usage_limit():
    result = runner.RunResult()
    runner.apply_event(result, {"type": "result", "is_error": True, "result": "rate limited by the tool"})
    assert result.limit_reset_at is None


def test_describe_tool_uses_plain_japanese():
    assert runner.describe_tool("Read", {"file_path": "a.py"}) == "読んでいる: a.py"
    assert runner.describe_tool("Grep", {"pattern": "x"}) == "調べている: x"


# 契約の上限の読み取り（書き方が版によって違う）


@pytest.mark.parametrize("text, expected", [
    ("Claude AI usage limit reached|1789830000", 1789830000.0),
    ("You've hit your session limit · resets 6:30pm (Asia/Tokyo)", "18:30"),
    ("5-hour limit reached ∙ resets 3pm", "15:00"),
    ("Weekly limit reached · resets 9am", "09:00"),
    ("You've hit your session limit", runner.UNKNOWN_LIMIT_RESET),
    ("ふつうのエラー: ファイルがありません", None),
])
def test_parse_limit_reads_every_wording(text, expected):
    from datetime import datetime

    now = datetime(2026, 9, 20, 12, 0).timestamp()
    got = runner.parse_limit(text, now)
    if isinstance(expected, str):
        assert datetime.fromtimestamp(got).strftime("%H:%M") == expected
        assert got > now  # 明ける時刻は必ず先
    else:
        assert got == expected


def test_parse_limit_moves_to_tomorrow_when_the_time_has_passed():
    from datetime import datetime

    now = datetime(2026, 9, 20, 20, 0).timestamp()
    got = runner.parse_limit("You've hit your session limit · resets 6:30pm (Asia/Tokyo)", now)
    assert datetime.fromtimestamp(got).strftime("%m/%d %H:%M") == "09/21 18:30"


def test_research_runner_loads_only_the_research_plugin(config):
    """担当外の plugin（大学・仕事）を、同じ claude に読ませない。"""
    ws = themes.resolve(config, "vlm")
    cmd = runner.build_command(config, ws, None)

    loaded = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--plugin-dir"]
    assert loaded == [str(config.repo_root / "plugin" / "research")]


def test_agent_plugin_dir_refuses_an_unknown_agent(config):
    assert config.agent_plugin_dir("course").name == "course"
    with pytest.raises(ValueError, match="未知のagent"):
        config.agent_plugin_dir("voice")


def test_research_runner_uses_only_the_scoped_notion_mcp(config):
    """研究の Notion は、研究ホームだけを操作できるゲートウェイ経由。ほかの MCP は読み込まない。"""
    ws = themes.resolve(config, "vlm")
    cmd = runner.build_command(config, ws, None)

    mcp = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["research-notion"]
    assert mcp["url"] == config.notion_gateway_url
    assert mcp["headers"]["Authorization"] == "Bearer ${KEI_AGENT_NOTION_GATEWAY_TOKEN}"
    assert "--strict-mcp-config" in cmd


def test_research_settings_allow_the_gateway_tools(config):
    ws = themes.resolve(config, "vlm")
    allow = guard.build_settings(config, ws)["permissions"]["allow"]

    assert "mcp__research-notion" in allow


def test_research_env_carries_the_gateway_token_but_not_the_notion_token(config):
    env = runner.build_env(config, {
        "PATH": "/bin",
        "NOTION_TOKEN": "ntn_raw",
        "KEI_AGENT_NOTION_GATEWAY_TOKEN": "scoped",
    }, "C1", "1.2")

    assert "NOTION_TOKEN" not in env
    assert env["KEI_AGENT_NOTION_GATEWAY_TOKEN"] == "scoped"
