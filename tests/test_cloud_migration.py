"""クラウドへ移した担当の旧設定・予約を、Mac で再実行しない。"""

import time
from dataclasses import replace

import pytest
from fakes import make_assistant

from kei_agent.configuration import agents_table, schedules_table
from kei_agent.conversation.hands import Hands, HandsError
from kei_agent.scheduling.schedule import Scheduler


def test_old_ai_columns_do_not_restart_cloud_actors():
    parsed = agents_table.parse(
        "module,enabled,channels,engine,model,effort,folder,claude_account,engines\n"
        "course,true,course,claude,old-model,high,/tmp/course,/tmp/account,claude codex\n"
        "knowledge,true,knowledge,codex,old-model,high,/tmp/knowledge,,codex\n")
    assert parsed["agents"] == {}
    assert parsed["modules"] == ["course", "knowledge"]
    assert parsed["folders"]["course"] == "/tmp/course"
    assert parsed["channels"] == {"course": ["course"], "knowledge": ["knowledge"]}


def test_old_distribution_schedules_are_not_enabled():
    assert schedules_table.parse(
        "name,enabled,time\nreading,true,07:00\nliterature,true,07:00\nmaintenance,true,22:00\n"
    ) == {"maintenance": "22:00"}
    assert not {"reading", "literature"} & schedules_table.known_names().keys()


async def test_deferred_old_distribution_is_finished_without_replay(config, store, monkeypatch):
    assistant, slack = make_assistant(config, store)
    scheduler = Scheduler(config, store, assistant)
    for name in ("reading", "literature"):
        store.defer_run("schedule", {"name": name, "day": "2026-09-26", "provider": "claude"}, 1)

    async def unexpected(*args):
        pytest.fail("移行済みの配信を Mac で再実行してはいけない")

    monkeypatch.setattr(scheduler, "run_or_defer", unexpected)
    await scheduler.catch_up_deferred(time.time())
    assert not store.due_deferred("schedule", time.time())
    assert slack.posted() == []


@pytest.mark.parametrize("workspace", ["course", "knowledge"])
async def test_cloud_run_is_refused_without_leaving_a_ticket_or_thread(config, store, workspace):
    assistant, _ = make_assistant(config, store)
    hands = Hands(assistant)
    with pytest.raises(HandsError, match="頼めません"):
        await hands.run(workspace, "調べて", conversation="cloud-question")
    assert hands.records.items("ticket") == []
    assert store.get_thread("mcp", "cloud-question") is None


@pytest.mark.parametrize("name", ["course", "knowledge"])
def test_replacement_actor_keeps_its_configuration(monkeypatch, name):
    """同名の独自担当を登録した場合は、宣言された AI 設定を尊重する。"""
    from kei_agent.framework import modules

    known = modules.known()
    replacement = replace(known[name], actor=known["work"].actor)
    monkeypatch.setattr(modules, "known", lambda: {**known, name: replacement})
    parsed = agents_table.parse(
        "module,enabled,channels,engine,model,effort,engines\n"
        f"{name},true,,codex,,,claude codex\n")
    assert parsed["agents"][name]["provider"] == "codex"
    assert parsed["agents"][name]["engines"] == "claude codex"


@pytest.mark.parametrize("name", ["reading", "literature"])
def test_declared_schedule_can_reuse_retired_name(monkeypatch, tmp_path, name):
    """旧配信の名前でも、現在のモジュールが宣言する処理なら設定できる。"""
    from kei_agent.framework import modules

    spec = modules.ModuleSpec(
        name="custom", label="拡張", description="拡張", path=tmp_path, builtin=False,
        schedules=(modules.ScheduleSpec(name, "配信", "09:00"),))
    known = {**modules.known(), "custom": spec}
    monkeypatch.setattr(modules, "known", lambda: known)
    assert schedules_table.parse(f"name,enabled,time\n{name},true,10:00\n") == {name: "10:00"}
    assert schedules_table.parse(f"name,enabled,time\n{name},false,10:00\n") == {name: ""}
