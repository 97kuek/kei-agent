"""3つのエージェントに共通の、provider を1回動かす流れ（kei_agent_a2a.run と SkillExecutor.answer）。"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")

from kei_agent import runner
from kei_agent.agents import FIELDS
from kei_agent.model_classifier import UsageLimited
from kei_agent.model_policy import UseCase
from kei_agent_a2a import envelope, run
from kei_agent_course.executor import CourseExecutor
from kei_agent_research.executor import ResearchExecutor
from kei_agent_work.executor import WorkExecutor


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
        return ResearchExecutor(config, pueue=object(), store=store)
    return {"course": CourseExecutor, "work": WorkExecutor}[kind](config, store)


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
    ("research", UseCase.RESEARCH_EXTRACT), ("course", UseCase.COURSE_EXPLAIN), ("work", UseCase.WORK_DECIDE)])
async def test_every_agent_answers_ask_the_same_way(kind, use_case, config, store, ran):
    ask = {"prompt": "調べて", "session_id": "s-0", "channel": "C1", "thread_ts": "1.2",
           "use_case": use_case.value, "provider": "claude", "channel_name": "vlm"}
    updater = _Updater()

    await _executor(kind, config, store).handle(updater, {"skill": "ask"}, json.dumps(ask))

    (request, prompt), = ran
    assert updater.state == "completed"
    assert prompt == "調べて"                                    # 指示書は system prompt で渡し、依頼の文に混ぜない
    assert (request.recipe.actor, request.recipe.use_case, request.session_id) == (kind, use_case, "s-0")
    assert (request.channel, request.thread_ts) == ("C1", "1.2")
    reply = updater.envelope()
    assert reply["ok"] and reply["data"]["session_id"] == "s-1"
    assert set(reply["data"]) <= set(FIELDS)


@pytest.mark.parametrize("kind", ["course", "work"])
async def test_course_and_work_run_in_their_own_stable_workspace(kind, config, store, ran):
    ask = {"prompt": "調べて", "use_case": {"course": "course_explain", "work": "work_decide"}[kind],
           "provider": "codex"}
    await _executor(kind, config, store).handle(_Updater(), {"skill": "ask"}, json.dumps(ask))
    await _executor(kind, config, store).handle(_Updater(), {"skill": "ask"}, json.dumps(ask))

    first, second = (request.workspace.cwd for request, _ in ran)
    assert first == second == (config.course_root if kind == "course" else config.state_dir / "agents" / "work")


async def test_research_runs_in_the_theme_workspace(config, store, ran):
    ask = {"prompt": "図を作って", "use_case": "research_execute", "provider": "claude",
           "channel_name": "vlm", "allowed_domains": ["example.com"]}
    await _executor("research", config, store).handle(_Updater(), {}, json.dumps(ask))

    (request, _), = ran
    assert request.workspace.cwd == config.research_root / "vlm"
    assert request.workspace.allowed_domains == ("example.com",)


async def test_ask_without_a_use_case_is_classified_by_the_agents_classifier(config, store, ran, monkeypatch):
    asked = []

    async def classify(_config, _store, actor, prompt, *, provider=None):
        asked.append((actor, prompt, provider))
        return UseCase.COURSE_DEGREE_PLAN

    monkeypatch.setattr(run, "classify", classify)
    await _executor("course", config, store).handle(
        _Updater(), {"skill": "ask"}, json.dumps({"prompt": "卒業まで何単位？", "provider": "claude"}))

    assert asked == [("course", "卒業まで何単位？", "claude")]
    assert ran[0][0].recipe.use_case is UseCase.COURSE_DEGREE_PLAN


async def test_classifier_limit_comes_back_as_a_limited_failure(config, store, ran, monkeypatch):
    async def classify(*_args, **_kwargs):
        raise UsageLimited(456.0)

    monkeypatch.setattr(run, "classify", classify)
    updater = _Updater()
    await _executor("work", config, store).handle(updater, {"skill": "ask"}, json.dumps({"prompt": "x"}))

    assert updater.state == "failed" and updater.envelope()["limit_reset_at"] == 456.0
    assert ran == []


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


async def test_failed_envelope_makes_the_a2a_task_fail():
    updater = _Updater()
    await run.finish(updater, envelope.failure("上限に達した"))
    assert updater.state == "failed"


def test_json_reply_takes_the_outer_array_even_when_items_hold_arrays():
    text = '```json\n[{"subject": "朝会", "attendees": [{"name": "A"}]}]\n```\n出典 [1, 2]'
    assert run.json_reply(text) == [{"subject": "朝会", "attendees": [{"name": "A"}]}]


def test_json_reply_prefers_the_array_that_holds_the_items():
    assert run.json_reply('注記 [これは説明] 本文 [{"subject": "朝会"}]') == [{"subject": "朝会"}]
    assert run.json_reply('[{"subject": "朝会"}]\n\n出典 [1, 2]') == [{"subject": "朝会"}]
    assert run.json_reply("予定はありません。[]") == []


def test_json_reply_reads_a_long_reply_without_trying_every_bracket_pair():
    noise = "[注] " * 3000
    items = [{"subject": f"会議{i}"} for i in range(300)]
    assert run.json_reply(noise + json.dumps(items, ensure_ascii=False)) == items


def test_json_object_tolerates_code_fences_and_preambles():
    text = 'こちらです。\n```json\n{"complete": true, "items": []}\n```'
    assert run.json_object(text, "items") == {"complete": True, "items": []}
    with pytest.raises(ValueError):
        run.json_object("予定はありません", "items")
