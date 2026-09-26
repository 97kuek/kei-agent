"""声のレイヤからの問い合わせ（本体の A2A の口、kei_agent.questions と Assistant.answer_question）。"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")

from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import runner
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager
from kei_agent.model_policy import UseCase


@pytest.fixture
def assistant(config, store, monkeypatch):
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    made = Assistant(config, store, FakeSlack({"C1": "vlm"}), JobManager(config, store, FakePueue()),
                     "xoxb-test", "UBOT")
    made.claude = claude
    return made


class _Agent:
    base_url = "http://127.0.0.1:8787"

    def __init__(self, text: str):
        self.text = text
        self.asked: list[dict] = []

    async def stream(self, skill, text="", params=None, on_progress=None):
        from kei_agent import a2a

        self.asked.append(json.loads(text))
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text="", status_text=json.dumps({
            "ok": True, "text": self.text, "data": {"text": self.text, "is_error": False},
            "limit_reset_at": None, "cost_usd": None}))


async def test_course_question_is_asked_read_only_and_finalized(assistant):
    agent = _Agent("<<kei-agent-final>>\n今日は2コマだよ\n<<kei-agent-final-end>>")
    assistant.agents["course"] = agent

    answer = await assistant.answer_question("course", "今日の授業は？")

    asked, = agent.asked
    assert asked["read_only"] is True and asked["use_case"] == UseCase.COURSE_EXPLAIN.value
    assert asked["prompt"].endswith("今日の授業は？")
    assert answer == "今日は2コマだよ"                           # marker は外し、Slack と同じ確認を通す


async def test_research_question_runs_read_only_in_the_theme(assistant):
    answer = await assistant.answer_question("research", "何を確かめていた？", "#vlm")

    call, = assistant.claude.calls
    assert call["cwd"] == assistant.config.research_root / "vlm"
    assert answer == "結果です"


async def test_research_question_needs_a_theme_and_nothing_runs(assistant):
    assert "研究テーマ" in await assistant.answer_question("research", "何を確かめていた？")
    assert "研究テーマ" in await assistant.answer_question("research", "何を確かめていた？", "course")
    assert assistant.claude.calls == []


async def test_unknown_actor_or_empty_question_is_answered_without_asking(assistant):
    assert "どれを調べるか" in await assistant.answer_question("hobby", "何？")
    assert "何を調べるか" in await assistant.answer_question("work", "  ")


async def test_broken_answer_is_replaced_by_the_fixed_failure_text(assistant):
    assistant.agents["work"] = _Agent("marker の無い答え")
    answer = await assistant.answer_question("work", "今日の会議は？")
    assert answer.startswith("⚠️") and "marker" not in answer


async def test_executor_passes_the_question_to_the_assistant():
    from kei_agent.questions import NO_QUESTION, QuestionExecutor

    class _Assistant:
        async def answer_question(self, actor, question, theme=""):
            return f"{actor}:{question}:{theme}"

    class _Updater:
        state, message = "", None

        def new_agent_message(self, parts):
            return parts

        async def complete(self, message):
            self.state, self.message = "completed", message

        async def failed(self, message):
            self.state, self.message = "failed", message

    executor = QuestionExecutor(_Assistant())
    done = _Updater()
    await executor.handle(done, {}, json.dumps({"actor": "work", "question": "会議は？"}))
    assert done.state == "completed" and json.loads(done.message[0].text)["text"] == "work:会議は？:"

    refused = _Updater()
    await executor.handle(refused, {}, "会議は？")
    assert refused.state == "failed" and NO_QUESTION in json.loads(refused.message[0].text)["text"]


async def test_the_endpoint_listens_only_on_localhost():
    from kei_agent import questions

    troubles = []

    class _Assistant:
        async def notify_trouble(self, text):
            troubles.append(text)

    await questions.serve(_Assistant(), "http://0.0.0.0:8786")
    assert troubles and "127.0.0.1" in troubles[0]


def test_config_reads_the_orchestrator_address(tmp_path):
    from kei_agent.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text('[a2a]\norchestrator = "http://127.0.0.1:8786"\n')
    assert load_config(path, env={}).a2a.orchestrator == "http://127.0.0.1:8786"
    path.write_text('[a2a]\norchestrator = 8786\n')
    with pytest.raises(ConfigError, match="orchestrator"):
        load_config(path, env={})
