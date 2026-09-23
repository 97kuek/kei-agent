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
    # テーマ名に `#` は付けない（`10_vlm` のようなチャンネル名とずれて見えるので）
    assert "*vlm*" in text and "#vlm" not in text and "`zenodo.org`" in text
    assert "Daily" in text
    assert "export.arxiv.org" not in text  # 基本の接続先は出さない
    pickers = [b["accessory"] for b in view["blocks"] if b.get("accessory", {}).get("type") == "timepicker"]
    assert len(pickers) == len(settings.SCHEDULE_NAMES)


def test_home_shows_agent_provider_controls(config, store):
    view = home.build_home(config, store, [], is_owner=True)
    text = _texts(view)
    assert "大学: Claude" in text
    controls = [block.get("accessory", {}) for block in view["blocks"]]
    assert any(element.get("action_id") == "kei_agent_home_provider:course" for element in controls)


def test_home_model_choices_come_from_configured_recipes(config, store):
    from dataclasses import replace

    from kei_agent.config import ModelRecipe

    config = replace(config, model_recipes={
        "routine": ModelRecipe("codex", "gpt-routine", "low"),
        "standard": ModelRecipe("codex", "gpt-standard", "high"),
        "deep": ModelRecipe("codex", "gpt-deep", "xhigh"),
    })
    view = home.build_home(config, store, [], is_owner=True)
    model_picker = next(
        element for block in view["blocks"] for element in block.get("elements", [])
        if element.get("action_id") == "kei_agent_home_model:research")

    assert [option["value"] for option in model_picker["options"]] == [
        "__default__", "gpt-routine", "gpt-standard", "gpt-deep"]


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


async def test_home_provider_action_changes_the_next_agent_run(env, store):
    assistant, _ = env
    await assistant.on_home_action(_action("kei_agent_home_provider:course", selected_option={"value": "codex"}))
    assert settings.agent_profile(assistant.config, store, "course").provider == "codex"


async def test_research_home_override_wins_over_the_automatic_deep_recipe(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm"})
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    settings.set_agent_profile(store, "research", "codex", "gpt-home", "high")
    seen = {}

    async def fake_run(run_config, ws, _prompt, *_args, on_activity=None, on_text=None):
        seen.update(model=ws.model, effort=ws.reasoning_effort, profile=run_config.agent_profiles["research"])
        return runner.RunResult(text="ok")

    monkeypatch.setattr(runner, "run_claude", fake_run)
    await assistant.run_claude(themes.resolve(config, "vlm"), "実験計画を設計して")

    assert (seen["model"], seen["effort"]) == ("", "")
    assert (seen["profile"].model, seen["profile"].reasoning_effort) == ("gpt-home", "high")


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


# いま動いているもの


def test_home_shows_what_is_running(config, store):
    import time

    store.start_run("C1", "10.1", "vlm", "message")
    finished = store.start_run("C1", "10.0", "vlm", "message")
    store.end_run(finished, False, None)
    job = store.add_job("r1", "C1", "10.1", str(config.research_root / "vlm"), "sweep", "scripts/sweep.py",
                        status="queued")
    store.update_job(job.id, pueue_id=3, status="running")
    store.upsert_thread("C5", "20.1", "research-overview", None)
    store.set_awaiting("C5", "20.1", True)

    text = _texts(home.build_home(config, store, ["vlm"], is_owner=True))

    assert "いま動いているもの" in text
    assert "#vlm" in text and "依頼" in text            # 動いている依頼
    assert "ジョブ 1「sweep」実行中" in text
    assert "#research-overview" in text and "返事待ち" in text
    assert str(int(time.time())) not in text            # 時刻ではなく経過時間で出す


def test_home_says_when_nothing_is_running(config, store):
    assert "いまは何も動いていません" in _texts(home.build_home(config, store, [], is_owner=True))


async def test_refresh_button_rebuilds_the_home(env):
    assistant, slack = env
    await assistant.on_home_action(_action(home.REFRESH_ACTION, "refresh"))
    assert _published(slack)["user_id"] == "UME"
