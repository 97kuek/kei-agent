"""App Home の操作で、空の選択が来ても落ちない。"""

import pytest
from fakes import FakeAI, home_action, make_assistant

from kei_agent.execution import runner
from kei_agent.storage import settings


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeAI())
    return make_assistant(config, store, {"C1": "vlm"})


async def test_cleared_choices_keep_the_previous_value(env, config, store):
    assistant, slack = env
    before = settings.selected_provider(config, store, "course")
    await assistant.on_home_action(home_action("kei_agent_home_provider:course", selected_option=None))
    assert settings.selected_provider(config, store, "course") == before
