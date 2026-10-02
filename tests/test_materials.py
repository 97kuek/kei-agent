"""頭に渡す材料（scheduling/materials.py）。手の口の読む道具（agenda・reading・recent・jobs）が返すもの。"""

import json
import time
from datetime import date, datetime

import pytest
from fakes import make_assistant, make_theme

from kei_agent.conversation.hands import Hands
from kei_agent.operations.hands_server import build_mcp
from kei_agent.scheduling import materials
from kei_agent.storage.records import Records
from kei_agent.workspaces.theme_files import append_thread_log

NOW = datetime(2026, 10, 2, 9, 0)


@pytest.fixture
def assistant(config, store):
    return make_assistant(config, store, {"C1": "vlm"})[0]


async def test_the_agenda_mixes_classes_meetings_and_deadlines_in_time_order(assistant, monkeypatch):
    async def agenda(days, kinds=None):
        return {"course": [
            {"kind": "class", "subject": "機械学習", "start": "2026-10-02T13:00", "end": "2026-10-02T14:30"},
            # 0:00 ちょうどの締切は、前の日の 24:00
            {"kind": "due", "title": "レポート", "course": "統計", "at": "2026-10-03T00:00"},
            {"kind": "due", "title": "遠い", "at": "2026-10-20T12:00"}],
            "work": [{"kind": "meeting", "subject": "定例", "start": "2026-10-02T10:00", "end": "2026-10-02T11:00",
                      "location": "会議室"}]}, ["知識"]

    monkeypatch.setattr(assistant, "module_agenda", agenda)
    found = await materials.agenda(assistant, 1, now=NOW)
    assert [(i["kind"], i["start"], i["title"]) for i in found["items"]] == [
        ("会議", "10:00", "定例"), ("授業", "13:00", "機械学習"), ("締切", "24:00", "レポート")]
    assert found["items"][0]["where"] == "会議室" and found["items"][2]["date"] == "2026-10-02"
    assert found["unread"] == ["知識"] and (found["from"], found["to"]) == ("2026-10-02", "2026-10-02")
    # 長さは 1〜14 日に収める
    assert (await materials.agenda(assistant, 99, now=NOW))["to"] == "2026-10-15"


async def test_reading_comes_from_the_module_with_likes(assistant):
    module = assistant.modules["knowledge"]
    today = date.today().isoformat()
    for ts, day, liked in (("1", today, time.time()), ("2", "2000-01-01", None)):
        module.core.records.put("post", f"C40:{ts}", {"channel": "C40", "ts": ts, "day": day, "liked_at": liked,
                                                      "page": None, "item": {"title": f"記事{ts}", "url": "https://x"}})
    found = await materials.reading(assistant, 1)
    assert [(i["title"], i["liked"], i["saved"]) for i in found["items"]] == [("記事1", True, False)]


def test_recent_shows_threads_runs_requests_and_jobs(assistant, config, store):
    ws = make_theme(config)
    since = time.time() - 60
    store.upsert_thread("C1", "1759363200.0001", "vlm", None)
    append_thread_log(ws.cwd, "vlm", "1759363200.0001", "依頼者", "図を作って")
    append_thread_log(ws.cwd, "vlm", "1759363200.0001", "Kei Agent", "作りました")
    store.upsert_thread("C2", "1759363300.0001", "course", None)
    store.end_run(store.start_run("C1", "1", "vlm", "mention"), True, None, actor="research")
    store.start_run("C2", "2", "course", "mention")
    Records(store, "hands").put("ticket", "t-1", {"workspace": "work", "status": "done", "conversation": "c-1",
                                                  "started_at": time.time()})
    job = store.add_job("r1", "C1", "1", str(ws.cwd), "学習", "python train.py", "succeeded")
    store.update_job(job.id, finished_at=time.time())
    found = materials.recent(assistant, 1)
    threads = {t["workspace"]: t for t in found["threads"]}
    # 研究テーマは抜き出しつき、担当のスレッドは時刻だけ
    assert (threads["vlm"]["request"], threads["vlm"]["answer"]) == ("図を作って", "作りました")
    assert "request" not in threads["course"]
    assert {r["agent"]: (r["runs"], r["failed"], r["running"]) for r in found["runs"]} == {
        "research": (1, 1, 0), "course": (1, 0, 1)}
    assert found["hands"][0]["workspace"] == "work" and found["jobs"][0]["name"] == "学習"
    assert materials.recent(assistant, 1, now=since + 7200)["threads"] == []


def test_jobs_show_what_runs_and_what_just_finished(assistant, store, tmp_path):
    store.add_job("r1", "C1", "1", str(tmp_path / "vlm"), "学習", "x", "running")
    done = store.add_job("r2", "C1", "1", str(tmp_path / "vlm"), "評価", "x", "failed", detail="exit 1")
    store.update_job(done.id, finished_at=time.time())
    old = store.add_job("r3", "C1", "1", str(tmp_path / "vlm"), "昔", "x", "succeeded")
    store.update_job(old.id, finished_at=time.time() - 5 * 86400)
    found = materials.jobs(assistant)
    assert [j["name"] for j in found["finished"]] == ["評価"] and found["finished"][0]["detail"] == "exit 1"
    assert found["finished"][0]["workspace"] == "vlm"


async def test_the_read_tools_are_on_the_door(assistant):
    mcp = build_mcp(Hands(assistant))
    tools = {t.name: t.annotations for t in await mcp.list_tools()}
    assert all(tools[name].read_only_hint for name in ("agenda", "reading", "recent", "jobs"))
    result = await mcp.call_tool("jobs", {})
    assert result.structured_content == json.loads(result.content[0].text) == {"active": [], "finished": []}


def test_recent_survives_odd_threads_and_marks_stale_tickets_failed(assistant, store):
    store.upsert_thread("C1", "1.2.3", "vlm", None)
    Records(store, "hands").put("ticket", "t-old", {"ticket": "t-old", "workspace": "vlm", "status": "running",
                                                    "conversation": "c-1", "started_at": time.time()})
    found = materials.recent(assistant, 1)
    assert found["threads"][0]["started"] == "" and found["hands"][0]["status"] == "failed"


async def test_a_module_returning_the_wrong_shape_does_not_break_reading(assistant, monkeypatch):
    async def wrong(days):
        return ["not", "a", "dict"]

    monkeypatch.setattr(assistant.modules["knowledge"], "head_materials", wrong, raising=False)
    assert await materials.reading(assistant, 1) == {"items": []}
