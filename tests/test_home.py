import asyncio

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import home, runner, settings, themes
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "research-overview"})
    monkeypatch.setattr(runner, "run_claude", FakeClaude())
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    themes.ensure_workspace(themes.resolve(config, "vlm"))
    return assistant, slack


def _texts(view) -> str:
    out = []
    for b in view["blocks"]:
        out.append((b.get("text") or {}).get("text", ""))
        out += [e.get("text", "") for e in b.get("elements", []) if isinstance(e.get("text"), str)]
    return "\n".join(out)


def _published(slack):
    return [kw for name, kw in slack.calls if name == "views_publish"][-1]


def _action(action_id, value="", user="UME", **extra):
    return {"user": {"id": user}, "trigger_id": "trig", "actions": [{"action_id": action_id, "value": value, **extra}]}


def test_home_lists_theme_domains_and_schedule(config, store):
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    view = home.build_home(config, store, ["vlm"], is_owner=True)
    text = _texts(view)
    assert "#vlm" in text and "`zenodo.org`" in text
    assert "Daily" in text
    assert "export.arxiv.org" not in text  # 基本の接続先は出さない
    pickers = [b["accessory"] for b in view["blocks"] if b.get("accessory", {}).get("type") == "timepicker"]
    assert len(pickers) == len(settings.SCHEDULE_NAMES)


def test_home_for_someone_else_changes_nothing(config, store):
    view = home.build_home(config, store, ["vlm"], is_owner=False)
    assert "依頼者だけ" in _texts(view)
    assert not any(b.get("accessory") or b["type"] == "actions" for b in view["blocks"])


async def test_opening_home_publishes_it(env):
    assistant, slack = env
    await assistant.on_home_opened({"type": "app_home_opened", "user": "UME", "tab": "home"})
    assert _published(slack)["user_id"] == "UME"


async def test_remove_domain_from_home(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    await assistant.on_home_action(_action("kei_agent_home_remove_domain", "vlm\tzenodo.org"))
    assert settings.theme_domains(store, "vlm") == []
    assert "zenodo.org" not in _texts(_published(slack)["view"])


async def test_someone_else_cannot_change_settings(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    await assistant.on_home_action(_action("kei_agent_home_remove_domain", "vlm\tzenodo.org", user="USOMEONE"))
    assert settings.theme_domains(store, "vlm") == ["zenodo.org"]


async def test_change_time_and_toggle_from_home(env, config, store):
    assistant, slack = env
    await assistant.on_home_action(_action("kei_agent_home_time:daily", selected_time="07:30"))
    assert settings.schedule_time(config, store, "daily") == "07:30"
    await assistant.on_home_action(_action("kei_agent_home_toggle:daily", "daily"))
    assert settings.schedule_time(config, store, "daily") == ""
    await assistant.on_home_action(_action("kei_agent_home_toggle:daily", "daily"))
    assert settings.schedule_time(config, store, "daily") == "07:30"


async def test_add_domain_through_modal(env, store):
    assistant, slack = env
    await assistant.on_home_action(_action("kei_agent_home_add_domain", "add"))
    opened, = [kw for name, kw in slack.calls if name == "views_open"]
    assert opened["view"]["callback_id"] == home.ADD_DOMAIN_CALLBACK

    def submitted(theme, domain):
        return {"user": {"id": "UME"}, "view": {"state": {"values": {
            "theme": {"value": {"selected_option": {"value": theme}}},
            "domain": {"value": {"value": domain}}}}}}

    assert "domain" in await assistant.on_add_domain(submitted("vlm", "https://zenodo.org"))
    assert "theme" in await assistant.on_add_domain(submitted("nothere", "zenodo.org"))
    assert await assistant.on_add_domain(submitted("vlm", "*.githubusercontent.com")) is None
    assert settings.theme_domains(store, "vlm") == ["*.githubusercontent.com"]


async def test_archiving_the_channel_drops_theme_domains(env, store):
    assistant, slack = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    await assistant.on_channel_archive({"type": "channel_archive", "channel": "C1"})
    await asyncio.sleep(0)
    assert settings.theme_domains(store, "vlm") == []
