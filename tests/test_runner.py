import asyncio
import json
import os
import tomllib
from dataclasses import replace
from datetime import datetime

import pytest

from kei_agent import guard, router, runner, themes
from kei_agent.agent_policy import NOTION_READ_TOOLS, policy_of
from kei_agent.configuration.config import load_config
from kei_agent.execution_contract import resolve_contract
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve, resolve_classifier
from kei_agent.provider_permissions import CapabilityUnavailable, preflight
from kei_agent.storage.notion import gateway_client_token


def request(config, *, actor="research", provider="claude", use_case="research_execute",
            session_id=None, read_only=False):
    if actor == "router":
        ws = router.workspace(config)
    elif actor == "course":
        ws = themes.agent_workspace(config, "course")
    else:
        ws = themes.resolve(config, "vlm")
    return runner.ExecutionRequest(ws, resolve(actor, provider, use_case), session_id, "C1", "1.1", read_only)


def codex_configs(command):
    return [command[i + 1] for i, arg in enumerate(command) if arg == "--config"]


def codex_filesystem(command):
    value = next(item.split("=", 1)[1] for item in command
                 if item.startswith("permissions.kei_agent_scoped.filesystem="))
    return tomllib.loads("value=" + value)["value"]


def claude_settings(command):
    return json.loads(command[command.index("--settings") + 1])


def fake_bin(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\ncat > /dev/null\n" + body)
    path.chmod(0o755)
    return str(path)


CLAUDE_OK = 'printf \'{"type":"result","subtype":"success","session_id":"s1","result":"ok","is_error":false}\\n\'\n'


def test_settings_limit_theme_to_its_directory_and_block_secrets(config):
    """テーマの外へ書かせず、秘密情報の置き場所は sandbox（Bash）と Read の両方で塞ぐ。"""
    ws = themes.resolve(config, "vlm")
    settings = guard.build_settings(config, ws, policy_of("research"))
    allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
    assert f"Read(/{ws.cwd}/**)" in allow
    assert f"Edit(/{ws.cwd}/**)" in allow
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["network"]["allowedDomains"] == ["export.arxiv.org"]
    filesystem = settings["sandbox"]["filesystem"]
    assert filesystem["denyRead"] == [str(p) for p in config.deny_read]
    assert filesystem["allowWrite"] == [str(p) for p in config.allow_write]
    # sandbox は Bash にしか効かない。Read・Grep・Glob からも、同じ場所を読ませない
    for path in config.deny_read:
        assert f"Read(/{path}/**)" in deny and f"Read(/{path})" in deny
    # 研究ホームだけに届くゲートウェイは使える。アカウントに付いた Notion 連携はホームの外まで届くので断る
    assert "mcp__kei-notion" in allow
    assert "mcp__claude_ai_Notion" in deny


def test_settings_add_domains_allowed_for_the_theme(config):
    """Slack で許可した接続先は、基本の接続先に足して使う。"""
    ws = replace(themes.resolve(config, "vlm"), allowed_domains=("zenodo.org",))
    domains = guard.build_settings(config, ws, policy_of("research"))["sandbox"]["network"]["allowedDomains"]
    assert domains == ["export.arxiv.org", "zenodo.org"]


def test_settings_overview_reads_all_themes_but_writes_only_overview(config):
    ws = themes.resolve(config, "research-overview")
    allow = guard.build_settings(config, ws, policy_of("research"))["permissions"]["allow"]
    assert f"Read(/{config.research_root}/**)" in allow
    assert f"Edit(/{config.overview_dir}/**)" in allow
    assert f"Edit(/{config.research_root}/**)" not in allow


def test_default_deny_read_covers_keys_profiles_and_the_state_dir(tmp_path):
    """研究の Bash から、鍵・大学や仕事の連携を付けたプロファイル・状態の秘密情報を読ませない。"""
    toml = tmp_path / "config.toml"
    toml.write_text(f'state_dir = "{tmp_path / "state"}"\n')
    deny_read = load_config(toml, env={}).deny_read
    paths = [str(p) for p in deny_read]
    for suffix in ("/.ssh", "/.aws", "/.claude", "/.claude-personal", "/.claude-work"):
        assert any(p.endswith(suffix) for p in paths), suffix
    assert any("zsh/local" in p for p in paths)
    assert (tmp_path / "state" / "secrets").resolve() in deny_read


def test_command_resumes_session_and_ignores_user_settings(config):
    cmd = runner.build_command(config, request(config, session_id="sess-1"))
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    claude_settings(cmd)
    assert "--resume" not in runner.build_command(config, request(config))


def test_research_claude_command_loads_only_its_plugin_and_the_scoped_notion_mcp(config):
    """担当外の plugin（大学・仕事）とほかの MCP を読ませない。Notion は研究ホームだけのゲートウェイ経由。"""
    cmd = runner.build_command(config, request(config))

    loaded = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--plugin-dir"]
    assert loaded == [str(config.repo_root / "modules" / "research" / "plugin"), str(config.repo_root / "plugins" / "notion")]
    mcp = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["kei-notion"]
    assert mcp["url"] == config.notion_gateway_url
    assert mcp["headers"]["Authorization"] == "${KEI_AGENT_NOTION_GATEWAY_AUTH}"
    assert "--strict-mcp-config" in cmd


def test_agent_plugin_dir_refuses_an_unknown_agent(config):
    # 大学はモジュール。skill とフックは modules/course/plugin
    assert config.agent_plugin_dir("course") == config.repo_root / "modules" / "course" / "plugin"
    with pytest.raises(ValueError, match="未知のagent"):
        config.agent_plugin_dir("voice")


def test_codex_recipe_builds_a_scoped_jsonl_command(config):
    """Codex には解決したモデルと深さだけを1回渡し、粗い sandbox ではなく絞ったプロファイルで動かす。"""
    config = replace(config, codex_bin="codex-test")
    execution = request(config, provider="codex", session_id="thread-1")
    cmd = runner.build_command(config, execution)
    contract = resolve_contract(config, execution)

    assert cmd[:4] == ["codex-test", "exec", "--json", "--strict-config"]
    assert codex_filesystem(cmd)[str(config.research_root / "vlm")] == "write"
    assert cmd[cmd.index("--model") + 1] == "gpt-6-sol" and cmd.count("gpt-6-sol") == 1
    assert "model_reasoning_effort=high" in cmd and "gpt-5.6-terra" not in cmd
    assert "--plugin-dir" not in cmd and "--sandbox" not in cmd
    assert "--ignore-user-config" in cmd
    assert all(setting in cmd for setting in preflight(config, contract, "codex_cli").config_overrides)
    instruction = next(value for value in cmd if value.startswith("developer_instructions="))
    assert json.loads(instruction.split("=", 1)[1]) == contract.prompt_text
    assert cmd[-1] == "-"
    # 用途が変われば深さも変わる（ExecutionRequest にモデルや深さを上書きする欄は無い）
    design = runner.build_command(config, request(config, provider="codex", use_case="research_design"))
    assert "model_reasoning_effort=xhigh" in design
    assert {"model", "reasoning_effort"}.isdisjoint(execution.__dataclass_fields__)


def test_execution_request_rejects_a_forged_manual_recipe(config):
    forged = runner.ResolvedModel("research", "research_execute", "codex", "gpt-6-astra", "xhigh")
    with pytest.raises(ModelPolicyError, match="recipe"):
        runner.build_command(config, runner.ExecutionRequest(themes.resolve(config, "vlm"), forged, None, "C1", "1.1"))


def test_resolved_recipe_sends_claude_effort_only_when_enabled(config):
    thinking = runner.build_command(config, request(config))
    no_thinking = runner.build_command(config, request(config, actor="router", use_case=UseCase.ROUTING))

    assert thinking[thinking.index("--model"):thinking.index("--model") + 2] == ["--model", "claude-sonnet-5"]
    assert thinking[thinking.index("--effort"):thinking.index("--effort") + 2] == ["--effort", "high"]
    assert "--effort" not in no_thinking
    # 振り分けには研究の plugin も Notion も渡さない
    assert "research-notion" not in " ".join(no_thinking)
    assert str(config.agent_plugin_dir("research")) not in no_thinking


def test_codex_skills_are_scoped_to_the_current_agent_without_removing_user_skills(config, tmp_path):
    research = resolve_contract(config, request(config, provider="codex"))
    course = resolve_contract(config, request(config, actor="course", provider="codex", use_case="course_explain"))
    router_contract = resolve_contract(config, request(
        config, actor="router", provider="codex", use_case=UseCase.ROUTING, read_only=True))
    target = tmp_path / ".agents" / "skills"
    user_skill = target / "user-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("user-owned")

    runner.install_agent_skills(research, tmp_path)
    wandb = target / "managing-wandb"
    assert wandb.is_symlink()
    assert wandb.resolve() == config.agent_plugin_dir("research") / "skills" / "managing-wandb"
    assert not (target / "managing-academic-record").exists()
    runner.install_agent_skills(course, tmp_path)

    assert user_skill.is_dir()
    assert {path.name for path in target.iterdir() if path.is_symlink()} == {
        path.name for path in course.skill_dir.iterdir() if (path / "SKILL.md").is_file()
    } | {"keeping-notion-format"}                         # 大学は Notion を使うので、共通の skill も渡す
    # 振り分けに切り替えると、前に入れた担当の skill を外す
    runner.install_agent_skills(router_contract, tmp_path)
    assert not [path for path in target.iterdir() if path.is_symlink()]
    assert user_skill.is_dir()


def test_read_only_execution_removes_write_tools_and_keeps_only_notion_reads(config):
    settings = claude_settings(runner.build_command(config, request(config, read_only=True)))
    allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]

    assert "Bash" not in allow and "Bash" in deny
    assert not any(item.startswith("Edit(") for item in allow) and {"Edit", "Write"} <= set(deny)
    # ゲートウェイは読む道具だけ（書く道具も、サーバー全体の許可も渡さない）
    assert "mcp__kei-notion" not in allow
    assert [item for item in allow if item.startswith("mcp__kei-notion__")] == [
        f"mcp__kei-notion__{tool}" for tool in NOTION_READ_TOOLS]

    codex = runner.build_command(config, request(config, provider="codex", read_only=True))
    assert "write" not in codex_filesystem(codex).values()
    enabled = next(item for item in codex_configs(codex) if item.startswith("mcp_servers.kei-notion.enabled_tools="))
    assert json.loads(enabled.split("=", 1)[1]) == list(NOTION_READ_TOOLS)


def test_classifier_recipe_is_always_read_only_even_if_the_caller_omits_it(config, store):
    recipe = resolve_classifier(config, store, "research")
    command = runner.build_command(config, runner.ExecutionRequest(
        themes.resolve(config, "vlm"), recipe, None, "C1", "1.1"))

    assert "Bash" not in claude_settings(command)["permissions"]["allow"]
    # 分類は道具を持たない。MCP も何も読み込まない
    assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in command
    assert "--plugin-dir" not in command


def test_codex_research_command_uses_only_the_scoped_notion_gateway(config):
    configs = codex_configs(runner.build_command(config, request(config, provider="codex")))
    assert any("mcp_servers.kei-notion.url" in item and config.notion_gateway_url in item for item in configs)
    assert "mcp_servers.kei-notion.required=true" in configs
    # 無人で動くので、ゲートウェイの道具を呼ぶたびの承認は求めない（求めると Codex は断って止まる）
    assert 'mcp_servers.kei-notion.default_tools_approval_mode="approve"' in configs
    assert not any("enabled_tools" in item for item in configs)         # 書ける回は道具を絞らない
    assert any("env_http_headers" in item and "KEI_AGENT_NOTION_GATEWAY_AUTH" in item for item in configs)
    assert not any("NOTION_TOKEN" in item for item in configs)


def test_codex_router_command_is_untrusted_directory_safe_and_has_no_connectors(config):
    """振り分けは非gitの状態DBで動き、Google等のAppや研究Notionを触らない。"""
    command = runner.build_command(config, request(config, actor="router", provider="codex", use_case=UseCase.ROUTING))

    assert "--skip-git-repo-check" in command
    assert "write" not in codex_filesystem(command).values()
    configs = codex_configs(command)
    assert "apps._default.enabled=false" in configs
    assert 'web_search="disabled"' in configs
    assert not any("kei-notion" in item for item in configs)
    assert "--dangerously-bypass-hook-trust" not in command


def test_codex_shows_only_the_read_tools_of_the_apps_in_the_table(config):
    """Codex App は、表に書いた App の読む道具だけ。アップロードや共有の道具はモデルに見せない。"""
    box = next(app for app in policy_of("course").codex_apps if app.name == "Box")
    execution = request(config, actor="course", provider="codex", use_case="course_explain")
    configs = codex_configs(runner.build_command(config, execution, apps={"Box": "asdk_app_1"}))

    assert "apps._default.enabled=false" in configs
    assert "apps.asdk_app_1.default_tools_enabled=false" in configs
    assert 'apps.asdk_app_1.default_tools_approval_mode="approve"' in configs
    tools = tomllib.loads("tools=" + next(item.split("=", 1)[1] for item in configs
                                          if item.startswith("apps.asdk_app_1.tools=")))["tools"]
    assert set(tools) == set(box.tools) and "box.upload_file" not in tools
    assert all(value == {"enabled": True} for value in tools.values())


def test_codex_turns_off_tools_that_claude_agents_do_not_have(config):
    """サブエージェント・画像の生成・プラグインの導入は、どの担当にも渡さない。画像を開くのはファイルを読む担当だけ。"""
    def disabled(command):
        return {command[i + 1] for i, arg in enumerate(command) if arg == "--disable"}

    research = runner.build_command(config, request(config, provider="codex"))
    course = runner.build_command(config, request(config, actor="course", provider="codex", use_case="course_explain"))

    assert set(runner.CODEX_OFF_FEATURES) <= disabled(research) and "view_image" not in disabled(research)
    assert set(runner.CODEX_OFF_FEATURES) | {"view_image"} <= disabled(course)


def test_apply_codex_events_maps_thread_message_and_activities():
    """Codex の道具の経過も Claude と同じ言い方で出し、答えは回が終わったときの最後の文だけにする。"""
    result = runner.RunResult()
    assert runner.apply_codex_event(result, {"type": "thread.started", "thread_id": "t1"}) is None
    command = runner.apply_codex_event(result, {
        "type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}})
    mcp = runner.apply_codex_event(result, {"type": "item.started", "item": {
        "type": "mcp_tool_call", "server": "kei-notion", "tool": "search", "arguments": {"query": "授業"}}})
    search = runner.apply_codex_event(result, {"type": "item.completed", "item": {
        "type": "web_search", "query": "VLM benchmark"}})
    assert command == "実行している: pytest -q"
    assert mcp == "mcp__kei-notion__search"
    assert search == "Web で検索している: VLM benchmark"
    assert result.activities == [command, mcp, search]

    runner.apply_codex_event(result, {"type": "item.completed", "item": {"type": "agent_message", "text": "内部の途中経過"}})
    runner.apply_codex_event(result, {"type": "item.completed", "item": {"type": "agent_message", "text": "完了"}})
    assert result.text == ""
    runner.apply_codex_event(result, {"type": "turn.completed", "usage": {"total_cost_usd": 0.2}})
    assert (result.session_id, result.text, result.is_error) == ("t1", "完了", False)


@pytest.mark.parametrize("returncode", [1, -9])
def test_failed_or_killed_run_discards_even_a_result_text(returncode):
    result = runner.finalize_run_result(runner.RunResult(text="途中結果"), returncode=returncode)
    assert result.is_error and result.failure_kind == "runtime"
    assert result.text == ""


def test_codex_missing_rollout_is_a_missing_session():
    """Codex の resume で会話が見つからないときも、Slack の履歴から戻せるようにする。"""
    result = runner.finalize_run_result(runner.RunResult(
        is_error=True, errors=["Error: thread/resume failed: no rollout found for thread id abc"]), returncode=1)
    assert result.session_missing and result.failure_kind == "session_missing"


def test_system_prompt_warns_that_replies_do_not_auto_continue(config):
    """「続ける」と言い切って実際には止まる、という矛盾を防ぐための一文。"""
    for path in (config.repo_root / "prompts" / "system.md", config.repo_root / "modules" / "research" / "research.md"):
        text = path.read_text(encoding="utf-8")
        assert "自動で" in text and "続き" in text and "止まる" in text, path


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


def test_env_strips_kei_agent_tokens_and_every_notion_key():
    """Notion の鍵もゲートウェイの親の合言葉も、どの子（大学・仕事・Codex も）にも渡さない。"""
    env = guard.strip_env({
        "KEI_AGENT_A2A_TOKEN": "a2a-secret",
        "KEI_AGENT_FUTURE_TOKEN": "future-secret",
        "KEI_AGENT_NOTION_GATEWAY_TOKEN": "gateway",
        "KEI_AGENT_NOTION_GATEWAY_AUTH": "Bearer stray",
        "NOTION_TOKEN": "ntn_raw",
        "NOTION_COURSE_TOKEN": "ntn_course",
        "KEI_AGENT_CONFIG": "/tmp/config.toml",
    })
    assert env == {"KEI_AGENT_CONFIG": "/tmp/config.toml"}


@pytest.mark.parametrize("agent", ["research", "course", "work", "router", "improve", None])
def test_notion_agents_carry_only_their_home_token_never_the_master(config, agent):
    """子が持つのは自分のホームにしか届かない合言葉だけ。親の合言葉があれば全部のホームに届いてしまう。
    Notion を使わない担当には、合言葉を何も渡さない。"""
    env = runner.build_env(config, {
        "PATH": "/bin",
        "NOTION_TOKEN": "ntn_raw",
        "NOTION_COURSE_TOKEN": "ntn_course",
        "KEI_AGENT_NOTION_GATEWAY_TOKEN": "master",
        "KEI_AGENT_NOTION_GATEWAY_AUTH": "Bearer stray",
    }, "C1", "1.2", policy_of(agent) if agent else None)

    assert "NOTION_TOKEN" not in env and "NOTION_COURSE_TOKEN" not in env
    assert "KEI_AGENT_NOTION_GATEWAY_TOKEN" not in env
    assert not any("master" in value for value in env.values())
    if agent in ("research", "course"):
        assert env["KEI_AGENT_NOTION_GATEWAY_AUTH"] == f"Bearer {gateway_client_token('master', agent)}"
    else:
        assert "KEI_AGENT_NOTION_GATEWAY_AUTH" not in env


def test_claude_gateway_header_reads_the_variable_the_child_actually_gets(config):
    """合言葉そのものは子に渡さないので、ヘッダーは渡している GATEWAY_AUTH を展開する。"""
    header = runner.notion_mcp_config(config)["mcpServers"][runner.NOTION_MCP]["headers"]["Authorization"]
    env = runner.build_env(config, {"PATH": "/bin", runner.GATEWAY_TOKEN_ENV: "s3cret"}, "C1", "1.1", policy_of("research"))

    name = header.removeprefix("${").removesuffix("}")
    assert env[name] == f"Bearer {gateway_client_token('s3cret', 'research')}"
    assert runner.GATEWAY_TOKEN_ENV not in env


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


def test_missing_session_detected():
    result = runner.RunResult()
    runner.apply_event(result, {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "errors": ["No conversation found with session ID: x"],
    })
    assert result.session_missing


def test_describe_tool_uses_plain_japanese_and_truncates():
    assert runner.describe_tool("Read", {"file_path": "a.py"}) == "読んでいる: a.py"
    assert runner.describe_tool("Grep", {"pattern": "x"}) == "調べている: x"
    text = runner.describe_tool("WebSearch", {"query": "あ" * 200})
    assert text.startswith("Web で検索している: ") and text.endswith("…") and len(text) < 100


@pytest.mark.parametrize("event, expected", [
    # claude -p は上限に達すると `Claude AI usage limit reached|<エポック秒>` を返す
    ({"result": "Claude AI usage limit reached|1789800000"}, 1789800000),
    ({"errors": ["Claude AI usage limit reached"]}, runner.UNKNOWN_LIMIT_RESET),
    ({"result": "rate limited by the tool"}, None),
])
def test_apply_event_reads_the_usage_limit(event, expected):
    result = runner.RunResult()
    runner.apply_event(result, {"type": "result", "is_error": True, **event})
    assert result.limit_reset_at == expected


# 契約の上限の読み取り（書き方が版によって違う）


@pytest.mark.parametrize("text, now_hour, expected", [
    ("Claude AI usage limit reached|1789830000", 12, 1789830000.0),
    ("You've hit your session limit · resets 6:30pm (Asia/Tokyo)", 12, "09/20 18:30"),
    ("5-hour limit reached ∙ resets 3pm", 12, "09/20 15:00"),
    ("Weekly limit reached · resets 9am", 12, "09/21 09:00"),
    ("You've hit your session limit · resets 6:30pm (Asia/Tokyo)", 20, "09/21 18:30"),  # 過ぎていれば翌日
    ("You've hit your session limit", 12, runner.UNKNOWN_LIMIT_RESET),
    ("ふつうのエラー: ファイルがありません", 12, None),
])
def test_parse_limit_reads_every_wording(text, now_hour, expected):
    now = datetime(2026, 9, 20, now_hour, 0).timestamp()
    got = runner.parse_limit(text, now)
    if isinstance(expected, str):
        assert datetime.fromtimestamp(got).strftime("%m/%d %H:%M") == expected
        assert got > now  # 明ける時刻は必ず先
    else:
        assert got == expected


def test_failure_reason_says_what_happened_even_without_an_error_text():
    """時間切れや空の返事でも、ログと知らせに理由が残る（振り分けの失敗が空欄になっていた）。"""
    assert runner.RunResult(is_error=True, failure_kind="timeout").failure_reason() == "時間切れ"
    assert runner.RunResult(is_error=True, failure_kind="runtime").failure_reason() == (
        "実行の失敗：返事が空か、終了コードだけを残して止まった")
    assert runner.RunResult(is_error=True, failure_kind="quota", errors=["usage limit"]).failure_reason() == (
        "利用上限：usage limit")
    assert runner.RunResult(is_error=True, errors=["x" * 300]).failure_reason(10) == "x" * 10
    assert runner.RunResult(is_error=True).failure_reason() == "理由不明"


def test_an_expired_login_is_told_apart_from_other_failures():
    """ログインが切れた回は、答えの代わりに返った理由の文を残し、入り直し方を先頭に置く（2026-09-27）。"""
    said = "Failed to authenticate: OAuth session expired and could not be refreshed"
    result = runner.finalize_run_result(runner.RunResult(text=said, is_error=True), returncode=1)
    assert (result.failure_kind, result.text, result.errors) == ("login", "", [said])
    assert result.failure_reason().startswith("ログインが切れている：")
    help_ = runner.login_help("claude", {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-personal"})
    assert ".claude-personal" in help_ and "claude auth login" in help_ and "/Users" not in help_ and ":" not in help_
    assert "codex login" in runner.login_help("codex", {})
    # ほかの失敗は、今までどおり実行の失敗（途中の文は理由にしない）
    other = runner.finalize_run_result(runner.RunResult(text="途中の答え"), returncode=1)
    assert (other.failure_kind, other.errors) == ("runtime", [])


# 本物のプロセスを起動する（偽の claude・codex のシェルスクリプト）


async def run_fake(config, provider="claude", **kwargs):
    execution = request(config, provider=provider)
    themes.ensure_workspace(execution.workspace)
    return await asyncio.wait_for(runner.run_model(config, execution, "調べて", **kwargs), timeout=10)


async def test_run_claude_returns_even_if_a_left_over_process_holds_the_output(config, tmp_path, monkeypatch):
    """claude が終わっても、Bash が残したプロセスが出力を握っていることがある。そこで固まらない。

    直す前は stderr の EOF を待ち続けて、スレッドの順番待ちと並行枠を握ったままになっていた。
    """
    monkeypatch.setattr(runner, "EXIT_GRACE_SECONDS", 0.5)
    config = replace(config, claude_bin=fake_bin(tmp_path, "fake-claude.sh", "sleep 60 &\necho $! > left-over.pid\n" + CLAUDE_OK))

    result = await run_fake(config)

    assert result.text == "ok" and not result.is_error and not result.timed_out
    pid = int((themes.resolve(config, "vlm").cwd / "left-over.pid").read_text())
    for _ in range(50):  # 残ったプロセスも片づける
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"claude が残したプロセス {pid} が生きています")


async def test_run_claude_keeps_a_successful_result_even_if_it_had_to_be_killed(config, tmp_path, monkeypatch):
    """result まで届いたあと claude 自身が終わらず、猶予のあとで止めた回も、答えは捨てない。"""
    monkeypatch.setattr(runner, "EXIT_GRACE_SECONDS", 0.5)
    config = replace(config, claude_bin=fake_bin(tmp_path, "fake-claude-hang.sh", CLAUDE_OK + "exec sleep 60\n"))

    result = await run_fake(config)

    assert result.text == "ok" and not result.is_error


async def test_run_codex_reads_jsonl_and_installs_research_skills(config, tmp_path):
    """Codex JSONLの応答・進捗と、テーマ作業場の研究skillを同時に扱える。"""
    config = replace(config, codex_bin=fake_bin(tmp_path, "fake-codex.sh", (
        "printf '%s\\n' "
        "'{\"type\":\"thread.started\",\"thread_id\":\"thread-1\"}' "
        "'{\"type\":\"item.started\",\"item\":{\"type\":\"command_execution\",\"command\":\"pwd\"}}' "
        "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"完了\"}}' "
        "'{\"type\":\"turn.completed\"}'\n")))
    seen_activity: list[str] = []

    async def on_activity(activity: str) -> None:
        seen_activity.append(activity)

    result = await run_fake(config, "codex", on_activity=on_activity)

    assert (result.session_id, result.text, result.is_error) == ("thread-1", "完了", False)
    # 途中の文（未検証の agent_message）は流さず、道具の経過だけを流す
    assert seen_activity == ["実行している: pwd"]
    assert (themes.resolve(config, "vlm").cwd / ".agents" / "skills" / "managing-wandb").is_symlink()


async def test_codex_profile_canary_failure_stops_before_starting_the_model(config, monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise CapabilityUnavailable("Codex の権限を強制できません")

    async def should_not_start(*_args, **_kwargs):
        raise AssertionError("model process must not start")

    monkeypatch.setattr(runner, "verify_codex_profile", unavailable)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", should_not_start)

    result = await run_fake(config, "codex")

    assert result.is_error and result.failure_kind == "capability"
    assert result.text == ""


async def test_codex_nonzero_exit_never_returns_its_completed_message(config, tmp_path, monkeypatch):
    config = replace(config, codex_bin=fake_bin(tmp_path, "fake-codex-failed.sh", (
        "printf '%s\\n' "
        "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"途中結果\"}}' "
        "'{\"type\":\"turn.completed\"}'\n"
        "exit 1\n")))

    async def verified(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "verify_codex_profile", verified)

    result = await run_fake(config, "codex")

    assert result.is_error and result.failure_kind == "runtime"
    assert result.text == ""
