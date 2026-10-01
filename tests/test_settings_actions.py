"""App Home の操作で、空の選択が来ても落ちない。"""

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent.conversation.assistant import Assistant
from kei_agent.execution import runner
from kei_agent.execution.jobs import JobManager
from kei_agent.storage import settings


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm"})
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    return Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT"), slack


def _action(action_id, **extra):
    return {"user": {"id": "UME"}, "trigger_id": "trig", "actions": [{"action_id": action_id, **extra}]}


async def test_cleared_choices_keep_the_previous_value(env, config, store):
    assistant, slack = env
    before = settings.selected_provider(config, store, "course")
    await assistant.on_home_action(_action("kei_agent_home_provider:course", selected_option=None))
    assert settings.selected_provider(config, store, "course") == before
