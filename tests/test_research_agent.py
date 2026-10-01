"""オーケストレーター（Kei Agent 本体）と、研究の担当プロセス（研究のモジュールの agent.py。A2A サーバー）の往復。

本物のサーバーを 127.0.0.1 に立てて、claude の1回分を頼み、経過が流れてきて、結果が返るところまでを見る。
claude そのものは動かさず、偽の runner に差し替える。
"""

import asyncio
import contextlib
import json
import socket
from dataclasses import replace

import pytest

from kei_agent import agents, jobs, modules, runner
from kei_agent.a2a import Agent

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
pytest.importorskip("uvicorn")

TOKEN = "test-token"


@contextlib.asynccontextmanager
async def _serve(executor):
    """研究エージェントを 127.0.0.1 の空いている番地に立てて、住所を返す。"""
    import uvicorn

    from kei_agent_a2a import launch

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = launch.build_app(modules.builtin()["research"], base, TOKEN, executor=executor)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        await task


class FakeClaude:
    """runner.run_model の代わり。経過を流してから結果を返す。"""

    def __init__(self, result: runner.RunResult):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, config, request, prompt, on_activity=None):
        ws = request.workspace
        self.calls.append({"cwd": ws.cwd, "prompt": prompt, "session_id": request.session_id,
                           "channel": request.channel, "thread_ts": request.thread_ts,
                           "allowed_domains": ws.allowed_domains, "recipe": request.recipe})
        if on_activity:
            await on_activity("Bash: テスト")
        return self.result


@pytest.fixture
async def server(config, monkeypatch):
    """研究エージェントを立てて、(住所, 偽の claude) を返す。"""
    from kei_agent_modules.research.agent import Executor

    claude = FakeClaude(runner.RunResult(
        session_id="sess-9", text="できたよ", cost_usd=0.12,
        requested_domains=[("example.com", "データを取るため")]))
    monkeypatch.setattr(runner, "run_model", claude)
    async with _serve(Executor(config)) as base:
        yield base, claude


async def test_card_says_it_runs_claude_and_holds_the_jobs(server):
    base, _ = server
    card = await Agent(base, TOKEN).card()
    assert card["name"] == "Kei Agent（研究）"
    assert [s["id"] for s in card["skills"]] == [
        "ask", "submit-job", "list-jobs", "cancel-job", "forget-job"]


async def test_the_orchestrator_gets_the_result_and_the_progress(server, config):
    """結果は RunResult に戻り、途中の経過は on_activity に届く。"""
    from kei_agent import themes

    base, claude = server
    ws = replace(themes.resolve(config, "vlm"), allowed_domains=("example.com",))
    activities = []

    result = await agents.run_in_workspace(
        Agent(base, TOKEN, timeout=30), ws, "図を作って", "sess-1", "C1", "10.1", "research_execute",
        on_activity=_collect(activities))

    assert result.text == "できたよ" and result.session_id == "sess-9" and not result.is_error
    assert result.cost_usd == 0.12
    # JSON では組が配列になるので、戻してから接続先の許可に使う
    assert result.requested_domains == [("example.com", "データを取るため")]
    call, = claude.calls
    assert call["cwd"] == config.research_root / "vlm"
    assert call["prompt"] == "図を作って" and call["session_id"] == "sess-1"
    assert call["channel"] == "C1" and call["thread_ts"] == "10.1"
    assert call["allowed_domains"] == ("example.com",)
    # tool activity だけを流す。モデルの途中 text はオーケストレーターへも渡さない
    assert activities == ["Bash: テスト"]

    # 手動指定の用途（manual_fable）も、そのまま担当側で使われる
    await agents.run_in_workspace(Agent(base, TOKEN, timeout=30), ws, "難問を設計して", None, "", "", "manual_fable")
    assert claude.calls[-1]["recipe"].model == "claude-fable-5"


async def test_a_broken_request_or_a_channel_without_a_directory_is_refused(server):
    """作業用ディレクトリのないチャンネル（Kei Agent の改善）や、読めない依頼は断る。"""
    base, claude = server
    agent = Agent(base, TOKEN, timeout=30)
    task = await agent.ask("ask", json.dumps({"channel_name": "0-kei-agent", "prompt": "やって"}))
    assert not task.ok and "作業用ディレクトリがありません" in json.loads(task.answer)["text"]
    task = await agent.ask("ask", "これは JSON ではない")
    assert not task.ok and "prompt が要ります" in json.loads(task.answer)["text"]
    assert claude.calls == []


def test_to_result_keeps_only_what_it_knows():
    """知らない項目が増えても落ちない。組は JSON で配列になるので戻す。"""
    result = agents.to_result({"text": "できた", "requested_domains": [["example.com", "なぜ"]],
                                 "これは知らない": 1})
    assert result.text == "できた" and result.requested_domains == [("example.com", "なぜ")]


async def test_a_dead_agent_becomes_an_error_result(config):
    """つながらないエージェントは、エラーの RunResult になる（本体は普通の失敗として扱える）。"""
    from kei_agent import themes
    from kei_agent.a2a import Agent as Client

    ws = themes.resolve(config, "vlm")
    result = await agents.run_in_workspace(Client("http://127.0.0.1:1", timeout=3), ws, "やって", None, "", "",
                                           "research_execute")
    assert result.is_error and result.errors


def _collect(into):
    async def collect(value):
        into.append(value)
    return collect


# 長い処理（pueue のジョブ）は、研究エージェント側の待ち行列に入れる


class FakePueue:
    """jobs.Pueue の代わり。呼ばれた内容を覚えておく。"""

    def __init__(self):
        self.calls = []
        self.group_ready = False

    async def ensure_group(self):
        self.group_ready = True

    async def add(self, cwd, command, label):
        self.calls.append(("add", str(cwd), command, label))
        return 42

    async def kill(self, task_id):
        self.calls.append(("kill", task_id))

    async def remove(self, task_id):
        self.calls.append(("remove", task_id))

    async def tasks(self):
        return {42: {"status": "Running"}}


@pytest.fixture
async def job_server(config):
    """ジョブを受け取る研究エージェント（pueue は偽物）。"""
    from kei_agent_modules.research.agent import Executor

    pueue = FakePueue()
    async with _serve(Executor(config, pueue=pueue)) as base:
        yield base, pueue


async def test_jobs_go_through_the_agent(job_server, config):
    """本体は RemotePueue を、いままでの pueue と同じように使える。"""
    RemotePueue = jobs.RemotePueue

    base, pueue = job_server
    cwd = config.research_root / "vlm"
    cwd.mkdir(parents=True, exist_ok=True)
    remote = RemotePueue(Agent(base, TOKEN, timeout=30))

    await remote.ensure_group()
    assert await remote.add(cwd, "uv run x.py", "kei-agent-3") == 42
    assert await remote.tasks() == {42: {"status": "Running"}}
    await remote.kill(42)
    await remote.remove(42)

    assert pueue.group_ready                       # 最初の投入で待ち行列を用意する
    assert pueue.calls == [("add", str(cwd.resolve()), "uv run x.py", "kei-agent-3"),
                           ("kill", 42), ("remove", 42)]


async def test_a_job_outside_the_research_directory_is_refused(job_server):
    """渡された場所で何でも動かさない（研究テーマのディレクトリの中だけ）。"""
    RemotePueue = jobs.RemotePueue

    base, pueue = job_server
    remote = RemotePueue(Agent(base, TOKEN, timeout=30))
    with pytest.raises(RuntimeError, match="ジョブを動かしてよい場所ではありません"):
        await remote.add("/tmp", "rm -rf /", "kei-agent-9")
    assert pueue.calls == []


def test_the_research_root_itself_is_not_a_theme_directory(config):
    """研究テーマを並べた場所そのものは、どのテーマでもないので断る。テーマの中と overview は通す。"""
    from kei_agent_modules.research.agent import Executor

    executor = Executor(config, pueue=FakePueue())
    theme = config.research_root / "vlm"
    theme.mkdir(parents=True)
    config.overview_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="ジョブを動かしてよい場所ではありません"):
        executor._job_dir(str(config.research_root))
    assert executor._job_dir(str(theme)) == theme.resolve()
    assert executor._job_dir(str(config.overview_dir)) == config.overview_dir.resolve()
