from kei_agent_a2a import claude
from kei_agent_course.executor import CourseExecutor


async def test_course_question_does_not_duplicate_agent_guide_in_user_prompt(config, store, monkeypatch):
    seen = {}

    async def connector(_config, prompt, *_args, **_kwargs):
        seen["prompt"] = prompt
        return "<<kei-agent-final>>回答したよ<<kei-agent-final-end>>"

    async def finish(_updater, _envelope):
        pass

    monkeypatch.setattr(claude, "ask_connector", connector)
    executor = CourseExecutor(config, store)
    monkeypatch.setattr(claude, "finish", finish)

    await executor._ask(None, {}, "過去問は？")

    assert "過去問は？" in seen["prompt"]
    assert "あなたは Kei Agent の大学エージェント" not in seen["prompt"]
