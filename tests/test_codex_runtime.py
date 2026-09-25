import pytest

from kei_agent import codex_runtime


def test_probe_command_is_ephemeral_readonly_and_uses_a_fixed_prompt(tmp_path):
    """`--ephemeral` や read-only を外してプローブが状態を残す変更を捕捉する。"""
    command = codex_runtime.build_probe_command("codex-test", tmp_path)

    assert command[:5] == ["codex-test", "exec", "--json", "--sandbox", "read-only"]
    assert command[command.index("--cd") + 1] == str(tmp_path)
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert command[-1] == "-"


def test_probe_parser_requires_the_exact_acknowledgement():
    """任意の agent message を疎通成功と誤認する変更を捕捉する。"""
    result = codex_runtime.parse_probe_events([
        '{"type":"thread.started","thread_id":"thread-123"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"作業しました"}}',
    ])

    assert result.available is False
    assert result.thread_id == "thread-123"
    assert result.error == "Codex の固定プローブ応答を確認できませんでした"


def test_probe_parser_accepts_only_the_fixed_acknowledgement():
    """固定応答を返したログでも接続不可になる変更を捕捉する。"""
    result = codex_runtime.parse_probe_events([
        '{"type":"thread.started","thread_id":"thread-123"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"KEI_AGENT_CODEX_OK"}}',
    ])

    assert result.available is True
    assert result.thread_id == "thread-123"
    assert result.error is None


def test_probe_parser_rejects_a_failed_turn_after_the_acknowledgement():
    """失敗 turn を固定応答だけで成功に塗り替える変更を捕捉する。"""
    result = codex_runtime.parse_probe_events([
        '{"type":"thread.started","thread_id":"thread-123"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"KEI_AGENT_CODEX_OK"}}',
        '{"type":"turn.failed","error":{"message":"request failed"}}',
    ])

    assert result.available is False
    assert result.error == "Codex の固定プローブが失敗しました"


def test_mcp_parser_returns_enabled_names_only_not_connection_details():
    """無効 connector や URL を実行時の許可対象に混ぜる変更を捕捉する。"""
    names = codex_runtime.parse_mcp_names(
        '[{"name":"wandb","enabled":true,"transport":{"url":"https://private.example/token"}},'
        '{"name":"notion","enabled":false,"transport":{"url":"https://private.example/other"}}]'
    )

    assert names == frozenset({"wandb"})
    assert all("private.example" not in name for name in names)


async def test_strict_mcp_discovery_rejects_unreadable_config(monkeypatch):
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"private error"

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(codex_runtime.asyncio, "create_subprocess_exec", create)
    with pytest.raises(RuntimeError, match="MCP の設定"):
        await codex_runtime.discover_enabled_mcp_names_strict("codex")


async def test_run_probe_uses_jsonl_output_from_the_cli(tmp_path):
    """CLI JSONL を読まないために実プローブが常に失敗する変更を捕捉する。"""
    fake = tmp_path / "fake-codex.sh"
    fake.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "printf '%s\\n' "
        "'{\"type\":\"thread.started\",\"thread_id\":\"thread-123\"}' "
        "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"KEI_AGENT_CODEX_OK\"}}'\n"
    )
    fake.chmod(0o755)

    result = await codex_runtime.run_probe(str(fake))

    assert result.available is True
    assert result.thread_id == "thread-123"
    assert result.error is None
