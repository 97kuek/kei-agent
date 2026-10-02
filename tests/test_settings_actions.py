"""App Home では AI を変えない。前に出した画面の AI の選択が押されても、何も変わらない。"""

import pytest
from fakes import FakeAI, home_action, make_assistant

from kei_agent.execution import runner
from kei_agent.storage import settings


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeAI())
    return make_assistant(config, store, {"C1": "vlm"})


@pytest.mark.parametrize("option", [None, {"value": "codex"}])
async def test_an_old_ai_choice_changes_nothing(env, config, option):
    assistant, slack = env
    before = settings.selected_provider(config, "course")
    await assistant.on_home_action(home_action("kei_agent_home_provider:course", selected_option=option))
    assert settings.selected_provider(config, "course") == before
    assert not [name for name, _ in slack.calls if name == "views_publish"]
