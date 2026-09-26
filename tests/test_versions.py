"""担当が古い版のまま動き続けないようにする（2026-09-26 に大学の担当で起きた）。"""

from __future__ import annotations

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import assistant as assistant_module
from kei_agent import improve, runner, version
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager


class _Agent:
    base_url = "http://127.0.0.1:8787"

    def __init__(self, *versions):
        self.versions = list(versions)

    async def card(self):
        return {"name": "Kei Agent（大学）", "version": self.versions.pop(0) if len(self.versions) > 1
                else self.versions[0], "skills": []}


@pytest.fixture
def assistant(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeClaude())
    monkeypatch.setattr(version, "RUNNING", "new")
    monkeypatch.setattr(assistant_module, "STALE_RECHECK_SECONDS", 0)
    made = Assistant(config, store, FakeSlack({}), JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    made.troubles = []

    async def trouble(text):
        made.troubles.append(text)

    monkeypatch.setattr(made, "notify_trouble", trouble)
    return made


def test_versions_differ_only_when_both_are_known():
    assert version.differs("old", "new")
    assert not version.differs("new", "new")
    assert not version.differs(version.UNKNOWN, "new") and not version.differs("old", version.UNKNOWN)
    assert not version.differs("", "new")


async def test_a_stale_agent_is_restarted_at_startup(assistant, monkeypatch):
    restarted = []
    monkeypatch.setattr(improve, "restart_service", lambda name: restarted.append(name) or True)
    assistant.agents = {"course": _Agent("old", "new"), "work": _Agent("new")}

    await assistant.check_agents()
    while assistant.tasks:
        import asyncio
        await asyncio.gather(*list(assistant.tasks))

    assert restarted == ["course"]
    assert assistant.troubles == []


async def test_an_agent_that_stays_old_is_reported(assistant, monkeypatch):
    monkeypatch.setattr(improve, "restart_service", lambda name: True)
    assistant.agents = {"course": _Agent("old")}

    await assistant.check_agents()
    while assistant.tasks:
        import asyncio
        await asyncio.gather(*list(assistant.tasks))

    trouble, = assistant.troubles
    assert "course" in trouble and "deploy/restart-all.sh" in trouble


def test_installed_services_restart_the_gateway_first_and_leave_the_main_process(tmp_path):
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    for name in ("assistant", "course", "notion-gateway", "research", "voice", "work"):
        (agents_dir / f"com.kei-agent.{name}.plist").touch()

    assert improve.installed_services(tmp_path) == ["notion-gateway", "course", "research", "voice", "work"]
