"""手の口（MCP。conversation/hands.py）: 頭（Dots・Claude Code など）から、作業場で AI を動かしてもらう。

返すのは決まった項目（状態・本文・会話の番号・できたファイル）。担当・アカウント・届く範囲は作業場から決まり、
頭が選べるのは、表で許した AI と重さだけ。口は合言葉を確かめる。
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent.conversation import hands as hands_module
from kei_agent.conversation.assistant import Assistant
from kei_agent.conversation.hands import Hands, HandsError
from kei_agent.conversation.hands_server import build_app, build_mcp
from kei_agent.execution import runner
from kei_agent.execution.jobs import JobManager

FINAL = "<<kei-agent-final>>\n{}\n<<kei-agent-final-end>>"


@pytest.fixture
def hands(config, store, monkeypatch):
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    config = replace(config, agent_profiles={**config.agent_profiles,
                                             "research": replace(config.agent_profiles["research"],
                                                                 engines=("claude", "codex"))})
    assistant = Assistant(config, store, FakeSlack({"C1": "vlm"}), JobManager(config, store, FakePueue()),
                          "xoxb", "UBOT")
    (config.research_root / "vlm").mkdir(parents=True)
    return Hands(assistant), claude


def test_workspaces_list_themes_and_agents_with_what_the_head_may_choose(hands):
    h, _ = hands
    found = {item["name"]: item for item in h.workspaces()}
    assert found["vlm"]["kind"] == "研究テーマ" and found["vlm"]["engines"] == ["claude", "codex"]
    assert found["course"]["kind"] == "担当" and found["course"]["engines"] == ["claude"]
    assert found["vlm"]["weights"] == ["light", "normal", "deep"]


async def test_a_short_run_answers_in_place_and_a_conversation_continues(hands):
    h, claude = hands
    claude.answer(FINAL.format("図を作りました"), session_id="sess-9",
                  side_effect=lambda cwd: (cwd / "outputs").mkdir(exist_ok=True) or (cwd / "outputs" / "a.png").write_bytes(b"x"))
    first = await h.run("vlm", "図を作って", weight="deep")
    assert (first["status"], first["text"]) == ("done", "図を作りました")
    assert first["files"] == [{"path": "outputs/a.png", "bytes": 1}]
    call = claude.calls[-1]
    assert (call["actor"], call["use_case"], call["session_id"]) == ("research", "research_design", None)  # 重さ → 用途
    # 会話の番号を渡すと、同じ会話（セッション）の続き。渡さなければ新しい会話
    claude.answer(FINAL.format("色を変えました"))
    again = await h.run("vlm", "色を変えて", conversation=first["conversation"])
    assert again["status"] == "done" and claude.calls[-1]["session_id"] == "sess-9"
    claude.answer(FINAL.format("別の話"))
    await h.run("vlm", "別の話")
    assert claude.calls[-1]["session_id"] is None


async def test_questions_come_back_as_needs_input_and_failures_as_failed(hands):
    h, claude = hands
    claude.answer(FINAL.format("どちらにする？\n❓ 確認: A と B のどちら？"))
    assert (await h.run("vlm", "直して"))["status"] == "needs_input"
    claude.answer("", is_error=True, errors=["こわれた"])
    failed = await h.run("vlm", "直して")
    assert failed["status"] == "failed" and "こわれた" in failed["text"]


async def test_a_long_run_returns_a_ticket_and_status_shows_the_result(hands, monkeypatch):
    h, claude = hands
    monkeypatch.setattr(hands_module, "SHORT_SECONDS", 0.05)
    gate = asyncio.Event()
    real = runner.run_model

    async def slow(*args, **kwargs):
        await gate.wait()
        return await real(*args, **kwargs)
    monkeypatch.setattr(runner, "run_model", slow)
    claude.answer(FINAL.format("終わりました"))
    accepted = await h.run("vlm", "長い実験を回して")
    assert accepted["status"] == "accepted"
    assert h.status(accepted["ticket"])["status"] == "running"
    gate.set()
    await h._tasks[accepted["ticket"]]
    done = h.status(accepted["ticket"])
    assert (done["status"], done["text"], done["conversation"]) == ("done", "終わりました", accepted["conversation"])
    with pytest.raises(HandsError, match="見つかりません"):
        h.status("t-nothing")


@pytest.mark.parametrize(("args", "said"), [
    (("vlm", "直して", "heavy"), "重さは"),
    (("vlm", "   "), "空です"),
    (("course", "質問", "normal", "codex"), "使える AI は claude"),
    (("0-kei-agent", "直して"), "頼めません"),
])
async def test_what_the_head_cannot_choose_is_refused(hands, args, said):
    h, claude = hands
    with pytest.raises(HandsError, match=said):
        await h.run(*args)
    assert claude.calls == []


async def test_the_door_checks_the_password(hands):
    h, claude = hands
    app = build_app(h, "secret-token", "127.0.0.1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/health")).status_code == 200
        # OAuth は使わない（トンネルの点検は、合言葉なしで 404 を見る）
        for given in ({}, {"authorization": "Bearer secret-token"}):
            found = await client.get("/.well-known/oauth-protected-resource/mcp", headers=given)
            assert found.status_code == 404 and found.content == b""
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        assert (await client.post("/mcp", json=body, headers=headers)).status_code == 401
        wrong = await client.post("/mcp", json=body, headers={**headers, "authorization": "Bearer nope"})
        assert wrong.status_code == 401


async def test_the_tools_run_on_the_loop_that_owns_the_records(hands):
    """道具が別のスレッドで動くと、記録の SQLite が使えない（作ったスレッドでしか使えない）。"""
    h, claude = hands
    claude.answer(FINAL.format("済みました"))
    mcp = build_mcp(h)

    async def call(name, args):
        result = await mcp.call_tool(name, args)
        # 決まった項目のまま渡り、同じ中身の文字も付く
        assert result.structured_content == json.loads(result.content[0].text)
        return result.structured_content

    ticket = (await call("run", {"workspace": "vlm", "request": "まとめて"}))["ticket"]
    assert (await call("status", {"ticket": ticket}))["status"] == "done"
    assert (await call("workspaces", {}))["workspaces"]


async def test_only_the_tools_that_just_read_say_so(hands):
    h, _ = hands
    tools = {t.name: t.annotations for t in await build_mcp(h).list_tools()}
    assert tools["workspaces"].read_only_hint and tools["status"].read_only_hint
    assert tools["run"] is None or not tools["run"].read_only_hint


def test_the_tunnel_needs_the_door_and_a_tunnel_number(tmp_path):
    from fakes import write_config

    from kei_agent.configuration.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    write_config(path, '[hands]\nurl = "http://127.0.0.1:8785"\ntunnel = "tunnel_6abf27"\n')
    config = load_config(path, env={})
    assert (config.hands_url, config.hands_tunnel) == ("http://127.0.0.1:8785", "tunnel_6abf27")
    for text, said in (('[hands]\ntunnel = "tunnel_1"\n', "url"), ('[hands]\nurl = "http://127.0.0.1:1"\ntunnel = "x"\n',
                                                                    "tunnel_"),
                       ('[hands]\nurl = "http://0.0.0.0:1"\n', "127.0.0.1")):
        write_config(path, text)
        with pytest.raises(ConfigError, match=said):
            load_config(path, env={})
