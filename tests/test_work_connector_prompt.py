from kei_agent_a2a import claude
from kei_agent_work import connector


async def test_work_question_does_not_duplicate_agent_guide_in_user_prompt(config, store, monkeypatch):
    seen = {}

    async def ask(_config, prompt, *_args, **_kwargs):
        seen["prompt"] = prompt
        return "<<kei-agent-final>>回答したよ<<kei-agent-final-end>>"

    monkeypatch.setattr(claude, "ask_connector", ask)

    await connector.ask(config, "明日の予定は？", store=store)

    assert "明日の予定は？" in seen["prompt"]
    assert "あなたは Kei Agent の仕事エージェント" not in seen["prompt"]


async def test_work_question_preserves_custom_guide_path(config, store, tmp_path, monkeypatch):
    guide = tmp_path / "guide.md"
    guide.write_text("仕事の追加ルール", encoding="utf-8")
    seen = {}

    async def ask(_config, _prompt, *_args, **kwargs):
        seen.update(kwargs)
        return "<<kei-agent-final>>回答したよ<<kei-agent-final-end>>"

    monkeypatch.setattr(claude, "ask_connector", ask)

    await connector.ask(config, "明日の予定は？", prompt_path=guide, store=store)

    assert seen["instructions_path"] == guide
