"""声のレイヤからの問い合わせ（本体の A2A の口、kei_agent.conversation.questions と Assistant.answer_question）。"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")

from fakes import FakeAI, make_assistant

from kei_agent.execution import runner


@pytest.fixture
def assistant(config, store, monkeypatch):
    claude = FakeAI()
    monkeypatch.setattr(runner, "run_model", claude)
    made, _ = make_assistant(config, store, {"C1": "vlm"})
    made.claude = claude
    return made


class _Agent:
    base_url = "http://127.0.0.1:8787"

    def __init__(self, text: str):
        self.text = text
        self.asked: list[dict] = []

    async def stream(self, skill, text="", params=None, on_progress=None):
        from kei_agent.execution import a2a

        self.asked.append(json.loads(text))
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text="", status_text=json.dumps({
            "ok": True, "text": self.text, "data": {"text": self.text, "is_error": False},
            "limit_reset_at": None, "cost_usd": None}))


async def test_course_question_is_asked_read_only_and_finalized(assistant):
    agent = _Agent("<<kei-agent-final>>\n今日は2コマだよ\n<<kei-agent-final-end>>")
    assistant.agents["course"] = agent

    answer = await assistant.answer_question("course", "今日の授業は？")

    asked, = agent.asked
    assert asked["read_only"] is True and asked["use_case"] == "course_explain"
    assert asked["prompt"].endswith("今日の授業は？")
    assert answer == "今日は2コマだよ"                           # marker は外し、Slack と同じ確認を通す


async def test_research_question_runs_read_only_in_the_theme(assistant):
    answer = await assistant.answer_question("research", "何を確かめていた？", "#vlm")

    call, = assistant.claude.calls
    assert call["cwd"] == assistant.config.research_root / "vlm"
    assert answer == "結果です"


async def test_unclear_questions_are_answered_without_asking_anyone(assistant):
    """研究テーマが無い・担当が分からない・中身が空の問いは、どの AI も動かさずに聞き返す。"""
    assert "研究テーマ" in await assistant.answer_question("research", "何を確かめていた？")
    assert "研究テーマ" in await assistant.answer_question("research", "何を確かめていた？", "course")
    assert "どれを調べるか" in await assistant.answer_question("hobby", "何？")
    assert "何を調べるか" in await assistant.answer_question("work", "  ")
    assert assistant.claude.calls == []


async def test_broken_answer_is_replaced_by_the_fixed_failure_text(assistant):
    assistant.agents["work"] = _Agent("marker の無い答え")
    answer = await assistant.answer_question("work", "今日の会議は？")
    assert answer.startswith("⚠️") and "marker" not in answer


async def test_executor_passes_the_question_to_the_assistant():
    from kei_agent.conversation.questions import NO_QUESTION, QuestionExecutor

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
    from kei_agent.conversation import questions

    troubles = []

    class _Assistant:
        async def notify_trouble(self, text):
            troubles.append(text)

    await questions.serve(_Assistant(), "http://0.0.0.0:8786")
    assert troubles and "127.0.0.1" in troubles[0]


def test_config_reads_the_orchestrator_address(tmp_path):
    from kei_agent.configuration.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text('[a2a]\norchestrator = "http://127.0.0.1:8786"\n')
    assert load_config(path, env={}).a2a.orchestrator == "http://127.0.0.1:8786"
    path.write_text('[a2a]\norchestrator = 8786\n')
    with pytest.raises(ConfigError, match="orchestrator"):
        load_config(path, env={})
