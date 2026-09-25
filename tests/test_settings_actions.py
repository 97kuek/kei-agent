"""App Home の操作で、空の選択が来ても落ちない。"""

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import runner, settings
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm"})
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    return Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT"), slack


def _action(action_id, **extra):
    return {"user": {"id": "UME"}, "trigger_id": "trig", "actions": [{"action_id": action_id, **extra}]}


async def test_cleared_time_keeps_the_previous_time(env, config, store):
    assistant, slack = env
    await assistant.on_home_action(_action("kei_agent_home_time:daily", selected_time="07:30"))
    await assistant.on_home_action(_action("kei_agent_home_time:daily", selected_time=None))
    assert settings.schedule_time(config, store, "daily") == "07:30"
    # 表示を作り直して、元の時刻に戻して見せる
    assert [name for name, _ in slack.calls].count("views_publish") == 2


async def test_cleared_provider_is_ignored(env, config, store):
    assistant, slack = env
    before = settings.selected_provider(config, store, "course")
    await assistant.on_home_action(_action("kei_agent_home_provider:course", selected_option=None))
    assert settings.selected_provider(config, store, "course") == before
