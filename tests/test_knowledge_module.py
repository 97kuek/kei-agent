"""知識のモジュール（modules/knowledge/module.py）。本体の定期処理・MCP 操作から、窓口（kei_agent.api）を通して動く。"""

import asyncio
from datetime import date

import pytest
from fakes import FakeAI, FakeHub, FakeNotion, make_assistant

from kei_agent.api import NotionError
from kei_agent.conversation.hands import Hands, HandsError
from kei_agent.execution import runner
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler


@pytest.fixture
def env(config, store, monkeypatch):
    claude = FakeAI()
    monkeypatch.setattr(runner, "run_model", claude)
    channels = {"C1": "vlm", "C5": "0-overview", "C9": "0-kei-agent", "C40": "4-knowledge"}
    assistant, slack = make_assistant(config, store, channels,
                                      notion=FakeNotion(), hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack, claude


def knowledge(assistant):
    """本体が読み込んだ知識のモジュール（class Module）。"""
    return assistant.modules["knowledge"]


def add_post(assistant, channel, ts, day, item):
    """朝の読みもので出した1件の控え（run_schedule("reading") が残すものと同じ形）。"""
    module = knowledge(assistant)
    module.core.records.put("post", f"{channel}:{ts}", {"channel": channel, "ts": ts, "day": day, "item": item,
                                                        "liked_at": None, "page": None}, keep_days=30)


def test_knowledge_has_no_mac_actor_process_or_schedules():
    spec = modules.builtin()["knowledge"]
    assert spec.actor is None and spec.port is None and not spec.schedules


READING = [{"title": "LLM の話", "url": "https://zenn.dev/x", "source": "Zenn", "interests": ["AI"],
            "summary": "要約", "why": "AI に近い"},
           {"title": "RAG の話", "url": "https://zenn.dev/y", "source": "Zenn", "interests": ["AI"],
            "summary": "要約2", "why": "AI に近い"}]


async def test_saving_an_article_guides_the_next_picks_and_can_be_undone(env, store):
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", date.today().isoformat(), READING[0])
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert result == {"url": "https://zenn.dev/x", "saved": True, "liked": True, "errors": []}
    post = knowledge(assistant).core.records.get("post", "C40:40.1")
    assert post["page"] == "reading-1" and post["liked_at"] is not None
    assert store.module_record("knowledge", "post", "C40:40.1")["expires_at"] is None
    material = (await knowledge(assistant).head_materials(1))["reading"]
    assert material[0]["saved"] is True and material[0]["liked"] is True
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x", "saved": False})
    assert result == {"url": "https://zenn.dev/x", "saved": False, "liked": False, "errors": []}
    assert assistant.hub.trashed == ["reading-1"]
    assert store.module_record("knowledge", "post", "C40:40.1")["expires_at"] is not None
    material = (await knowledge(assistant).head_materials(1))["reading"]
    assert material[0]["saved"] is False and material[0]["liked"] is False


async def test_repeated_and_concurrent_saves_reuse_a_page_for_the_same_url(env):
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    add_post(assistant, "C40", "40.2", "2026-09-27", READING[0])
    first, second = await asyncio.gather(
        assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"}),
        assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"}))
    assert first["saved"] is True and second["saved"] is True
    assert len(assistant.hub.readings) == 1
    assert {p["page"] for p in knowledge(assistant).core.records.items("post")} == {"reading-1"}
    add_post(assistant, "C40", "40.3", "2026-09-28", READING[0])
    await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert len(assistant.hub.readings) == 1
    assert {p["page"] for p in knowledge(assistant).core.records.items("post")} == {"reading-1"}
    await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x", "saved": False})
    await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x", "saved": False})
    assert assistant.hub.trashed == ["reading-1"]
    assert all(p["page"] is None and p["liked_at"] is None for p in knowledge(assistant).core.records.items("post"))


async def test_saving_matches_the_exact_url_and_keeps_other_articles(env):
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    add_post(assistant, "C40", "40.2", "2026-09-26", {**READING[0], "url": "https://zenn.dev/x?new=1"})
    await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert knowledge(assistant).core.records.get("post", "C40:40.2")["page"] is None
    for saved in (True, False):
        with pytest.raises(ValueError, match="URL"):
            await assistant.module_head_action("save_reading", {"url": "LLM の話", "saved": saved})
    assert len(assistant.hub.readings) == 1 and assistant.hub.trashed == []


@pytest.mark.parametrize("params", [{}, {"url": ""}, {"url": 1},
                                   {"url": "https://zenn.dev/x", "saved": "false"},
                                   {"url": "https://zenn.dev/x", "saved": 1},
                                   {"url": "https://zenn.dev/x", "saved": None}])
async def test_saving_refuses_invalid_parameters_before_changing_records(env, params):
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    with pytest.raises(ValueError, match="URL|saved"):
        await assistant.module_head_action("save_reading", params)
    assert knowledge(assistant).core.records.get("post", "C40:40.1")["liked_at"] is None
    assert assistant.hub.readings == {}


@pytest.mark.parametrize("missing", ["db", "hub"])
async def test_without_a_reading_db_preferences_are_kept_but_saving_reports_failure(env, missing):
    scheduler, assistant, slack, _ = env
    if missing == "db":
        assistant.hub.has_reading_db = False
    else:
        assistant.hub = None
    add_post(assistant, "C40", "40.1", date.today().isoformat(), READING[0])
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert result["saved"] is False and result["liked"] is True and result["errors"]
    material = (await knowledge(assistant).head_materials(1))["reading"]
    assert material[0]["saved"] is False and material[0]["liked"] is True
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x", "saved": False})
    assert result["liked"] is False and result["errors"] == []


async def test_notion_save_failure_reports_failure_and_allows_retry(env, monkeypatch):
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    add = assistant.hub.add_reading

    def fail(*args):
        raise NotionError("保存できません")

    monkeypatch.setattr(assistant.hub, "add_reading", fail)
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert result["saved"] is False and result["liked"] is True and result["errors"]
    assert knowledge(assistant).core.records.get("post", "C40:40.1")["page"] is None
    monkeypatch.setattr(assistant.hub, "add_reading", add)
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert result["saved"] is True and result["errors"] == []
    assert len(assistant.hub.readings) == 1


@pytest.mark.parametrize("missing_hub", [False, True])
async def test_notion_trash_failure_keeps_the_saved_state_for_retry(env, monkeypatch, missing_hub):
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    hub = assistant.hub

    def fail(page):
        raise NotionError("ゴミ箱に入れられません")

    trash = hub.trash_page
    if missing_hub:
        assistant.hub = None
    else:
        monkeypatch.setattr(hub, "trash_page", fail)
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x", "saved": False})
    assert result["saved"] is True and result["liked"] is True and result["errors"]
    assert knowledge(assistant).core.records.get("post", "C40:40.1")["page"] == "reading-1"
    assistant.hub = hub
    monkeypatch.setattr(hub, "trash_page", trash)
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x", "saved": False})
    assert result["saved"] is False and result["liked"] is False and result["errors"] == []
    assert hub.trashed == ["reading-1"]


async def test_the_knowledge_channel_cannot_be_created_as_a_research_workspace(env, config):
    scheduler, assistant, slack, claude = env
    with pytest.raises(HandsError, match="担当のチャンネル"):
        await Hands(assistant).create_workspace("knowledge")
    assert assistant.notion.themes == {}
    assert not (config.research_root / "knowledge").exists()
