"""エージェントが起動する Claude の、秘密情報と後始末の扱い。"""

from __future__ import annotations

import asyncio
import os

import pytest

from kei_agent import settings
from kei_agent_a2a import claude, envelope


class _Process:
    def __init__(self, block: bool = False):
        self.pid = 4321
        self.returncode = None
        self.block = block
        self.waited = False

    async def communicate(self, value):
        if self.block:
            await asyncio.Event().wait()
        self.returncode = 0
        return b"[]", b""

    async def wait(self):
        self.waited = True
        self.returncode = -9
        return self.returncode


async def test_connector_passes_only_the_environment_it_needs(config, monkeypatch):
    """会社用 Claude へ Slack・Notion・Box など別ドメインの鍵を渡さない。"""
    seen = {}
    process = _Process()

    async def create(*command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "company-claude")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-secret")
    monkeypatch.setenv("NOTION_TOKEN", "notion-secret")
    monkeypatch.setenv("BOX_CLIENT_SECRET", "box-secret")

    assert await claude.ask_connector(config, "予定", ("calendar_search",),
                                      config.agent_plugin_dir("work")) == "[]"

    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "company-claude"
    assert not {"SLACK_BOT_TOKEN", "NOTION_TOKEN", "BOX_CLIENT_SECRET"} & seen["env"].keys()
    command = list(seen["command"])
    denied = command[command.index("--disallowedTools") + 1:]
    assert {"Read", "Glob", "Grep", "Bash", "Write", "WebFetch"} <= set(denied)


async def test_connector_reaps_the_process_after_a_timeout(config, monkeypatch):
    """タイムアウト後に Claude と子プロセスを止め、親プロセスを回収する。"""
    process = _Process(block=True)
    killed = []

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append(pid))

    with pytest.raises(claude.ConnectorError, match="返りませんでした"):
        await claude.ask_connector(config, "予定", ("calendar_search",),
                                   config.agent_plugin_dir("work"), timeout_minutes=0)

    assert killed == [process.pid]
    assert process.waited


async def test_connector_uses_codex_when_the_agent_profile_selects_it(config, store, monkeypatch):
    settings.set_agent_provider(store, "course", "codex")
    seen = {}

    async def codex(_config, _agent, _prompt, model, effort, _timeout):
        seen.update(model=model, effort=effort)
        return "Boxを確認しました"

    monkeypatch.setattr(claude, "ask_codex_app", codex)
    assert await claude.ask_connector(
        config, "資料は？", (), config.agent_plugin_dir("course"), store=store, agent="course"
    ) == "Boxを確認しました"
    assert seen == {"model": "gpt-6-luna", "effort": "medium"}


async def test_connector_does_not_fallback_to_claude_after_a_codex_error(config, store, monkeypatch):
    settings.set_agent_provider(store, "work", "codex")

    async def broken(*_args, **_kwargs):
        raise claude.ConnectorError("Outlook が未接続です")

    monkeypatch.setattr(claude, "ask_codex_app", broken)
    with pytest.raises(claude.ConnectorError, match="Outlook"):
        await claude.ask_connector(
            config, "会議は？", (), config.agent_plugin_dir("work"), store=store, agent="work"
        )


def test_json_reply_requires_a_json_array():
    """連携の障害文を「予定0件」と取り違えず、前置き中の角括弧には耐える。"""
    assert claude.json_reply('注記 [これは説明] 本文 [{"subject": "朝会"}]') == [{"subject": "朝会"}]
    with pytest.raises(claude.ConnectorError, match="JSON の配列"):
        claude.json_reply("Microsoft 365 への接続が拒否されました")


def test_json_reply_prefers_the_array_that_holds_the_items():
    """あとに出典の配列が付いていても、予定の入った配列を選ぶ（黙って0件にしない）。"""
    assert claude.json_reply('[{"subject": "朝会"}]\n\n出典 [1, 2]') == [{"subject": "朝会"}]
    # 本当に0件のときは、空の配列をそのまま返す
    assert claude.json_reply("予定はありません。[]") == []


def test_ask_prompt_takes_the_question_out_of_the_envelope():
    """オーケストレーターが添えた session_id やチャンネル ID を、質問に混ぜない。"""
    payload = '{"prompt": "過去問ある？", "session_id": null, "channel": "C1", "thread_ts": "1.2"}'
    assert claude.ask_prompt(payload) == "過去問ある？"
    # 素の文はそのまま。prompt の無い JSON も、質問として読めるように崩さない
    assert claude.ask_prompt("  今日の授業は？  ") == "今日の授業は？"
    assert claude.ask_prompt('{"foo": 1}') == '{"foo": 1}'
    assert claude.ask_prompt("") == ""


class _Updater:
    def __init__(self):
        self.state = ""
        self.message = None

    def new_agent_message(self, parts):
        return parts

    async def complete(self, message):
        self.state, self.message = "completed", message

    async def failed(self, message):
        self.state, self.message = "failed", message


async def test_failed_envelope_makes_the_a2a_task_fail():
    """封筒が ok:false なら、A2A の task も failed にする。"""
    updater = _Updater()
    await claude.finish(updater, envelope.failure("上限に達した"))
    assert updater.state == "failed"


def test_connector_loads_only_its_own_plugin(config):
    """大学の claude に仕事の skill を渡さない（その逆も）。"""
    command = claude.connector_command(config, ["mcp__read"], (), config.agent_plugin_dir("course"))

    loaded = [command[i + 1] for i, arg in enumerate(command) if arg == "--plugin-dir"]
    assert loaded == [str(config.repo_root / "plugin" / "course")]
    allowed = command[command.index("--allowedTools") + 1:command.index("--disallowedTools")]
    assert "Skill" in allowed and "mcp__read" in allowed


def test_connector_does_not_disallow_the_skill_tool(config):
    command = claude.connector_command(config, ["mcp__read"], (), config.agent_plugin_dir("work"))

    assert "Skill" not in command[command.index("--disallowedTools") + 1:]


def test_connector_instructions_use_only_agent_guide(config):
    instructions = claude.connector_instructions(config, "course")

    assert instructions == (config.repo_root / "prompts" / "course.md").read_text(encoding="utf-8")
    assert "research-notion" not in instructions
    command = claude.connector_command(config, (), (), config.agent_plugin_dir("course"),
                                       instructions=instructions)
    assert command[command.index("--append-system-prompt") + 1] == instructions


async def test_codex_app_receives_agent_guide_as_developer_instructions(config, monkeypatch):
    seen = {}

    class FakeAppServer:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, prompt, _policy, _model, _effort, *, instructions, cwd, profile):
            seen.update(prompt=prompt, instructions=instructions, cwd=cwd, profile=profile)
            return claude.runner.RunResult(text="結果")

    monkeypatch.setattr(claude, "AppServerClient", FakeAppServer)
    assert await claude.ask_codex_app(config, "course", "過去問は？", "gpt-6-luna", "medium", 3) == "結果"
    assert seen["prompt"] == "過去問は？"
    assert seen["instructions"] == claude.connector_instructions(config, "course")
    assert seen["cwd"] == config.state_dir / "codex-connectors" / "course"
    assert "write" not in seen["profile"].filesystem.values()


def test_codex_connector_workspace_exposes_only_its_agent_skills(config):
    workspace = claude.prepare_codex_connector_workspace(config, "course")
    skill_root = workspace / ".agents" / "skills"

    assert workspace == config.state_dir / "codex-connectors" / "course"
    assert (skill_root / "managing-academic-record").is_symlink()
    assert not (skill_root / "managing-wandb").exists()
