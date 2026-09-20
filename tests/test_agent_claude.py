"""エージェントが起動する Claude の、秘密情報と後始末の扱い。"""

from __future__ import annotations

import asyncio
import os

import pytest

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

    assert await claude.ask_connector(config, "予定", ("calendar_search",)) == "[]"

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
        await claude.ask_connector(config, "予定", ("calendar_search",), timeout_minutes=0)

    assert killed == [process.pid]
    assert process.waited


def test_json_reply_requires_a_json_array():
    """連携の障害文を「予定0件」と取り違えず、前置き中の角括弧には耐える。"""
    assert claude.json_reply('注記 [これは説明] 本文 [{"subject": "朝会"}]') == [{"subject": "朝会"}]
    with pytest.raises(claude.ConnectorError, match="JSON の配列"):
        claude.json_reply("Microsoft 365 への接続が拒否されました")


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
