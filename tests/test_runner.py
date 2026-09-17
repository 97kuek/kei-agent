import json

from ezra import runner, themes


def test_settings_limit_theme_to_its_directory(config):
    ws = themes.resolve(config, "vlm")
    settings = runner.build_settings(config, ws)
    allow = settings["permissions"]["allow"]
    assert f"Read(/{ws.cwd}/**)" in allow
    assert f"Edit(/{ws.cwd}/**)" in allow
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["network"]["allowedDomains"] == ["export.arxiv.org"]


def test_settings_overview_reads_all_themes_but_writes_only_overview(config):
    ws = themes.resolve(config, "research-overview")
    allow = runner.build_settings(config, ws)["permissions"]["allow"]
    assert f"Read(/{config.research_root}/**)" in allow
    assert f"Edit(/{config.research_root / '_overview'}/**)" in allow
    assert f"Edit(/{config.research_root}/**)" not in allow


def test_command_resumes_session_and_ignores_user_settings(config):
    ws = themes.resolve(config, "vlm")
    cmd = runner.build_command(config, ws, "sess-1")
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    json.loads(cmd[cmd.index("--settings") + 1])
    assert "--resume" not in runner.build_command(config, ws, None)


def test_env_strips_secrets_and_adds_thread(config):
    base = {
        "PATH": "/bin",
        "SLACK_BOT_TOKEN": "xoxb-secret",
        "SLACK_APP_TOKEN": "xapp-secret",
        "NOTION_TOKEN": "ntn_secret",
        "EZRA_ALLOWED_USER_ID": "UME",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "parent",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep",
    }
    env = runner.build_env(config, base, "C1", "123.456")
    assert "SLACK_BOT_TOKEN" not in env and "SLACK_APP_TOKEN" not in env and "NOTION_TOKEN" not in env
    assert "EZRA_ALLOWED_USER_ID" not in env
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep"
    assert env["EZRA_CHANNEL"] == "C1" and env["EZRA_THREAD_TS"] == "123.456"
    assert env["EZRA_PLUGIN_DIR"].endswith("plugin")


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
    assert activity == "Bash: 一覧を見る"
    runner.apply_event(result, {
        "type": "result", "subtype": "success", "session_id": "s1", "result": "完了",
        "is_error": False, "total_cost_usd": 0.1, "duration_ms": 1000,
    })
    assert (result.session_id, result.text, result.is_error, result.cost_usd) == ("s1", "完了", False, 0.1)
    assert result.activities == ["Bash: 一覧を見る"]


def test_missing_session_detected():
    result = runner.RunResult()
    runner.apply_event(result, {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "errors": ["No conversation found with session ID: x"],
    })
    assert result.session_missing


def test_describe_tool_truncates():
    text = runner.describe_tool("WebSearch", {"query": "あ" * 200})
    assert text.startswith("Web検索: ") and text.endswith("…") and len(text) < 100
