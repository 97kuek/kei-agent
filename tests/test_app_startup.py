"""本体と定期処理は、Slack の接続状態に依存せず起動する。"""

from unittest.mock import AsyncMock

import pytest
from fakes import FakePueue

from kei_agent.conversation import service
from kei_agent.conversation.outbox import Outbox
from kei_agent.operations import app
from kei_agent.scheduling import schedule


@pytest.mark.parametrize("tokens", [{}, {"SLACK_BOT_TOKEN": "unused", "SLACK_APP_TOKEN": "unused"}])
async def test_startup_always_uses_the_notification_outbox(config, store, monkeypatch, tokens):
    monkeypatch.setenv("KEI_AGENT_ALLOWED_USER_ID", "UME")
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in tokens.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(app, "load_config", lambda: config)
    monkeypatch.setattr(app, "Store", lambda _: store)
    queue = FakePueue()
    queue.ensure_group = AsyncMock()
    monkeypatch.setattr(app.jobs, "queue", lambda _: queue)
    monkeypatch.setattr(app, "AsyncApp", lambda **_: pytest.fail("Slack には接続しない"), raising=False)
    monkeypatch.setattr(service, "load_notion", lambda _: None)
    monkeypatch.setattr(service, "load_hub", lambda _: None)
    started = []

    async def run(_config, _store, assistant):
        started.append(assistant)

    monkeypatch.setattr(app, "run_until_restart", run)
    await app.serve()

    assert len(started) == 1 and isinstance(started[0].slack, Outbox)


async def test_manual_schedule_runs_without_slack_tokens(config, store, monkeypatch, capsys):
    from kei_agent.configuration import config as configuration

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr(configuration, "load_config", lambda: config)
    monkeypatch.setattr(schedule, "Store", lambda _: store)
    queue = FakePueue()
    queue.ensure_group = AsyncMock()
    monkeypatch.setattr(schedule.jobs, "queue", lambda _: queue)
    monkeypatch.setattr(service, "load_notion", lambda _: None)
    monkeypatch.setattr(service, "load_hub", lambda _: None)

    async def run(self, name, day, *, record):
        assert isinstance(self.assistant.slack, Outbox)
        return {"status": "done", "name": name, "record": record}

    monkeypatch.setattr(schedule.Scheduler, "run_task", run)
    await schedule._run_once("maintenance", False)
    assert '"status": "done"' in capsys.readouterr().out
