"""担当が古い版のまま動き続けないようにする（2026-09-26 に大学の担当で起きた）。"""

from __future__ import annotations

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import assistant as assistant_module
from kei_agent import runner, updates, version
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


@pytest.mark.parametrize(("course", "restarted", "reported"), [
    (("old", "new"), ["course"], False),   # 起動のときに古い担当を入れ直す
    (("old",), ["course"], True),          # 入れ直しても古いままなら、直し方を添えて知らせる
])
async def test_a_stale_agent_is_restarted_and_reported_if_it_stays_old(assistant, monkeypatch, course, restarted,
                                                                         reported):
    import asyncio

    done = []
    monkeypatch.setattr(updates, "restart_service", lambda name: done.append(name) or True)
    assistant.agents = {"course": _Agent(*course), "work": _Agent("new")}

    await assistant.check_agents()
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks))

    assert done == restarted
    assert len(assistant.troubles) == int(reported)
    assert all("course" in trouble and "deploy/restart-all.sh" in trouble for trouble in assistant.troubles)


def test_installed_services_restart_the_gateway_first_and_leave_the_main_process(tmp_path):
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    for name in ("assistant", "course", "notion", "research", "voice", "work"):
        (agents_dir / f"com.kei-agent.{name}.plist").touch()

    assert updates.installed_services(tmp_path) == ["notion", "course", "research", "voice", "work"]
