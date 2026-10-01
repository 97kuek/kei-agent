"""3つのエージェントに共通の、provider を1回動かす流れ（kei_agent_a2a.run と SkillExecutor.answer）。"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")

from kei_agent.conversation.agents import FIELDS
from kei_agent.execution import runner
from kei_agent.execution.model_classifier import UsageLimited
from kei_agent_a2a import envelope, run
from kei_agent_modules.course.agent import Executor as CourseExecutor
from kei_agent_modules.research.agent import Executor as ResearchExecutor
from kei_agent_modules.work.agent import Executor as WorkExecutor


class _Updater:
    def __init__(self):
        self.state = ""
        self.message = None
        self.progress = []

    def new_agent_message(self, parts):
        return parts

    async def update_status(self, _state, message=None):
        self.progress.append(message)

    async def complete(self, message):
        self.state, self.message = "completed", message

    async def failed(self, message):
        self.state, self.message = "failed", message

    def envelope(self) -> dict:
        return json.loads(self.message[0].text)


def _executor(kind, config, store):
    if kind == "research":
        return ResearchExecutor(config, store, pueue=object())
    if kind == "work":
        # 仕事はモジュールの担当。名前は共通の起動コマンドが入れる（kei_agent_a2a.launch）
        executor = WorkExecutor(config, store)
        executor.agent = "work"
        return executor
    # 大学もモジュールの担当。名前は共通の起動コマンドが入れる（kei_agent_a2a.launch）
    executor = CourseExecutor(config, store)
    executor.agent = "course"
    return executor


@pytest.fixture
def ran(monkeypatch):
    """runner.run_model の代わり。受け取った実行要求と prompt を残す。"""
    seen = []

    async def run_model(_config, request, prompt, **_kwargs):
        seen.append((request, prompt))
        return runner.RunResult(provider=request.recipe.provider, session_id="s-1",
                                text="<<kei-agent-final>>答え<<kei-agent-final-end>>")

    monkeypatch.setattr(run.runner, "run_model", run_model)
    return seen


@pytest.mark.parametrize(("kind", "use_case"), [
    ("research", "research_extract"), ("course", "course_explain"), ("work", "work_decide")])
async def test_every_agent_answers_ask_the_same_way(kind, use_case, config, store, ran):
    """研究はテーマの作業場で、大学と仕事はいつも同じ自分の作業場で動く。"""
    ask = {"prompt": "調べて", "session_id": "s-0", "channel": "C1", "thread_ts": "1.2",
           "use_case": str(use_case), "provider": "claude", "channel_name": "vlm",
           "allowed_domains": ["example.com"]}
    updater = _Updater()

    await _executor(kind, config, store).handle(updater, {"skill": "ask"}, json.dumps(ask))

    (request, prompt), = ran
    assert updater.state == "completed"
    assert prompt == "調べて"                                    # 指示書は system prompt で渡し、依頼の文に混ぜない
    assert (request.recipe.actor, request.recipe.use_case, request.session_id) == (kind, use_case, "s-0")
    assert (request.channel, request.thread_ts) == ("C1", "1.2")
    assert request.workspace.cwd == {"research": config.research_root / "vlm", "course": config.course_root,
                                     "work": config.state_dir / "agents" / "work"}[kind]
    if kind == "research":
        assert request.workspace.allowed_domains == ("example.com",)
    reply = updater.envelope()
    assert reply["ok"] and reply["data"]["session_id"] == "s-1"
    assert set(reply["data"]) <= set(FIELDS)


async def test_ask_without_a_use_case_is_classified_by_the_agents_classifier(config, store, ran, monkeypatch):
    asked = []

    async def classify(_config, _store, actor, prompt, *, provider=None):
        asked.append((actor, prompt, provider))
        return "course_degree_plan"

    monkeypatch.setattr(run, "classify", classify)
    await _executor("course", config, store).handle(
        _Updater(), {"skill": "ask"}, json.dumps({"prompt": "卒業まで何単位？", "provider": "claude"}))

    assert asked == [("course", "卒業まで何単位？", "claude")]
    assert ran[0][0].recipe.use_case == "course_degree_plan"


async def test_classifier_limit_comes_back_as_a_limited_failure(config, store, ran, monkeypatch):
    async def classify(*_args, **_kwargs):
        raise UsageLimited(456.0)

    monkeypatch.setattr(run, "classify", classify)
    updater = _Updater()
    await _executor("work", config, store).handle(updater, {"skill": "ask"}, json.dumps({"prompt": "x"}))

    assert updater.state == "failed" and updater.envelope()["limit_reset_at"] == 456.0
    assert ran == []

    # ok: false の封筒は、A2A のタスクも failed にする（completed だと頼んだ側が気づけない）
    updater = _Updater()
    await run.finish(updater, envelope.failure("上限に達した"))
    assert updater.state == "failed"


@pytest.mark.parametrize("text", ["過去問ある？", "{}", '{"prompt": ""}'])
async def test_ask_needs_a_json_request_with_a_prompt(text, config, store, ran):
    updater = _Updater()
    await _executor("course", config, store).handle(updater, {"skill": "ask"}, text)

    assert updater.state == "failed" and run.NO_PROMPT in updater.envelope()["text"]
    assert ran == []


async def test_execute_sends_only_the_fields_the_orchestrator_reads(config, monkeypatch):
    """検証前の途中の文（_final_candidate）など、受け取る側が使わない項目は封筒に入れない。"""
    result = runner.RunResult(text="できた")
    result._final_candidate = "検証前の文"

    async def run_model(*_args, **_kwargs):
        return result

    monkeypatch.setattr(run.runner, "run_model", run_model)
    ws = type("W", (), {"cwd": config.research_root, "channel_name": "vlm"})()
    recipe = type("R", (), {"provider": "claude"})()
    payload = json.loads(await run.execute(config, ws, {"prompt": "x"}, _Updater(), recipe))

    assert set(payload["data"]) <= set(FIELDS)
    assert "検証前の文" not in json.dumps(payload, ensure_ascii=False)


async def test_the_recipe_comes_back_in_the_envelope(config, store, monkeypatch):
    """担当のプロセスで動かした担当・用途・モデル・effort は、封筒の data で本体に戻る（本体が記録に残す）。"""
    from kei_agent.conversation import agents

    async def inner(_config, request, prompt, on_activity=None):
        return runner.RunResult(provider=request.recipe.provider, session_id="s-1",
                                text="<<kei-agent-final>>答え<<kei-agent-final-end>>")

    monkeypatch.setattr(runner, "_run_model", inner)
    ask = {"prompt": "調べて", "session_id": "s-0", "channel": "C1", "thread_ts": "1.2", "use_case": "course_explain",
           "provider": "claude", "channel_name": "course"}
    updater = _Updater()
    await _executor("course", config, store).handle(updater, {"skill": "ask"}, json.dumps(ask))
    result = agents.to_result(updater.envelope()["data"])
    assert result.recipe_fields() == {"actor": "course", "use_case": "course_explain", "provider": "claude",
                                      "model": "claude-sonnet-5", "effort": "medium"}
