"""MCP の時間計測と Toggl・Notion への送信、直接計測の取り込み。"""

import asyncio
import sys
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from fakes import FakeAI, FakeHub, make_assistant

from kei_agent.execution import runner
from kei_agent.framework import modules
from kei_agent.scheduling.timelog import TogglAmbiguousWrite, TogglError
from kei_agent.storage.notion import NotionError
from kei_agent.testing.kit import settle
from kei_agent_modules.time import entries, importer


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeAI())
    channels = {"C1": "1-vlm", "C2": "2-course", "C3": "3-work", "C4": "2-linear-algebra", "C9": "0-kei-agent"}
    assistant, slack = make_assistant(config, store, channels, hub=FakeHub())
    module = assistant.modules["time"]
    # Toggl の設定は、使うテストだけが入れる
    monkeypatch.setattr(sys.modules[type(module).__module__], "load_toggl", lambda: None)
    return assistant, module, slack


def use_toggl(monkeypatch, module, toggl):
    monkeypatch.setattr(sys.modules[type(module).__module__], "load_toggl", lambda: toggl)


class RecordingToggl:
    def __init__(self, calls, fail=None):
        self.calls = calls
        self.fail = fail

    def record_completed(self, project, description, started_at, seconds):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("toggl", project, seconds))


async def measure(module, channel, theme, domain="research", minutes=25):
    """25分測って止めた記録（止めたあとの送信まで）。"""
    entry, _ = module.entries.start("UME", domain, channel, theme, started_at=time.time() - minutes * 60)
    stopped = module.entries.stop("UME", ended_at=entry.started_at + minutes * 60)
    await module._sync(stopped)
    return module.entries.entry(stopped.id)


async def timer(module, action="status", **params):
    return await module.head_action("timer", {"action": action, **params})


async def review_entry(module):
    entry, _ = module.entries.start("UME", "research", "C1", "vlm", started_at=time.time() - 1500)
    module.entries.stop("UME", ended_at=entry.started_at + 1500)
    return module.entries.set_delivery(entry.id, toggl_state="needs_review")


async def test_timer_switches_and_returns_complete_entries_without_cards(env):
    assistant, module, slack = env
    started = await timer(module, "start", domain="research", label="vlm")
    running = started["running"]
    assert running["description"] == "研究 / vlm"
    assert running["id"] and running["memo"] == "" and running["toggl_state"] == "pending"
    assert running["notion_state"] == "pending" and started["needs_review"] == []
    switched = await timer(module, "start", domain="course", label="線形代数")
    assert switched["running"]["description"] == "大学 / 線形代数"
    assert switched["stopped"]["id"] == running["id"]
    assert module.entries.active("UME").channel == "C2"
    stopped = await timer(module, "stop")
    assert stopped["running"] is None and stopped["stopped"]["description"] == "大学 / 線形代数"
    assert stopped["stopped"]["notion_state"] == "done"
    assert (await timer(module))["running"] is None and slack.posted() == []
    assert assistant.hub.recorded[-1][2] == "線形代数"


@pytest.mark.parametrize("params", [
    {"action": "start", "domain": "play", "label": "x"},
    {"action": "start", "domain": "research", "label": " "},
    {"action": "jump"}, {"action": "memo", "memo": " "},
    {"action": "resolve", "resolution": "recorded"},
    {"action": "resolve", "entry_id": "x"},
    {"action": "resolve", "entry_id": "x", "resolution": "retry"},
])
async def test_timer_rejects_incomplete_and_invalid_operations(env, params):
    _, module, _ = env
    with pytest.raises(ValueError):
        await module.head_action("timer", params)
    assert module.entries.active("UME") is None


async def test_memo_appends_and_updates_finished_records(env):
    assistant, module, _ = env
    await timer(module, "start", domain="research", label="vlm")
    first = await timer(module, "memo", memo=" 論文を読んだ ")
    assert first["running"]["memo"] == "論文を読んだ"
    second = await timer(module, "memo", memo="実験した")
    assert second["running"]["memo"] == "論文を読んだ\n実験した"
    stopped = await timer(module, "stop")
    entry_id = stopped["stopped"]["id"]
    await timer(module, "memo", entry_id=entry_id, memo="結果を整理した")
    assert module.entries.entry(entry_id).memo == "論文を読んだ\n実験した\n結果を整理した"
    assert module.entries.entry(entry_id).notion_state == "done"
    assert len(assistant.hub.recorded) == 2


async def test_memo_rejects_missing_records_and_other_owners(env):
    _, module, _ = env
    other, _ = module.entries.start("OTHER", "research", "C1", "vlm")
    for params in ({"memo": "x"}, {"memo": "x", "entry_id": "missing"},
                   {"memo": "x", "entry_id": other.id}):
        with pytest.raises(ValueError):
            await timer(module, "memo", **params)
    assert module.entries.entry(other.id).memo == ""


async def test_status_shows_only_owners_stopped_records_needing_confirmation(env):
    _, module, _ = env
    own = await review_entry(module)
    other, _ = module.entries.start("OTHER", "research", "C1", "vlm")
    module.entries.stop("OTHER")
    module.entries.set_delivery(other.id, toggl_state="needs_review")
    status = await timer(module)
    assert [e["id"] for e in status["needs_review"]] == [own.id]
    assert status["needs_review"][0]["toggl_state"] == "needs_review"
    assert status["needs_review"][0]["minutes"] == 25
    assert (await timer(module, "start", domain="work", label="定例"))["needs_review"] == status["needs_review"]


@pytest.mark.parametrize("resolution", ["recorded", "missing"])
async def test_resolve_resends_only_confirmed_missing_entries(env, monkeypatch, resolution):
    assistant, module, _ = env
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls))
    entry = await review_entry(module)
    status = await timer(module, "resolve", entry_id=entry.id, resolution=resolution)
    assert status["needs_review"] == [] and module.entries.entry(entry.id).sent
    assert assistant.hub.recorded == [(entry.id, "research", "vlm", 25, "Slack")]
    assert calls == ([] if resolution == "recorded" else [("toggl", "研究 / vlm", 1500)])
    with pytest.raises(ValueError):
        await timer(module, "resolve", entry_id=entry.id, resolution="missing")
    assert len(assistant.hub.recorded) == 1


async def test_resolve_leaves_other_owners_running_and_unreviewed_records_unchanged(env):
    _, module, _ = env
    other, _ = module.entries.start("OTHER", "research", "C1", "vlm")
    module.entries.stop("OTHER")
    module.entries.set_delivery(other.id, toggl_state="needs_review")
    running, _ = module.entries.start("UME", "research", "C1", "vlm")
    module.entries.set_delivery(running.id, toggl_state="needs_review")
    for entry_id in (other.id, running.id, "missing"):
        before = module.entries.entry(entry_id)
        with pytest.raises(ValueError):
            await timer(module, "resolve", entry_id=entry_id, resolution="recorded")
        assert module.entries.entry(entry_id) == before
    plain = module.entries.stop("UME")
    module.entries.set_delivery(plain.id, toggl_state="pending")
    with pytest.raises(ValueError):
        await timer(module, "resolve", entry_id=plain.id, resolution="missing")


async def test_ambiguous_toggl_write_waits_without_automatic_resend(env, monkeypatch):
    assistant, module, slack = env
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls, fail=TogglAmbiguousWrite("POST: TimeoutError")))
    entry = await measure(module, "C1", "vlm")
    assert entry.toggl_state == "needs_review" and assistant.hub.recorded == []
    notice = slack.posted()[-1]
    assert entry.id in notice["text"] and not notice.get("blocks")
    use_toggl(monkeypatch, module, RecordingToggl(calls))
    await module._look_around()
    assert calls == [] and (await timer(module))["needs_review"][0]["id"] == entry.id


async def test_parallel_retry_and_resolve_avoid_duplicate_delivery(env, monkeypatch):
    assistant, module, _ = env
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls))
    entry = await review_entry(module)
    entered, release = asyncio.Event(), asyncio.Event()
    original = module.core.to_thread

    async def delayed(func, *args):
        if getattr(func, "__name__", "") == "record_completed":
            entered.set()
            await release.wait()
        return await original(func, *args)

    monkeypatch.setattr(module.core, "to_thread", delayed)
    resolving = asyncio.create_task(timer(module, "resolve", entry_id=entry.id, resolution="missing"))
    await asyncio.wait_for(entered.wait(), 2)
    retrying = asyncio.create_task(module._sync(entry))
    competing = asyncio.create_task(timer(module, "resolve", entry_id=entry.id, resolution="missing"))
    release.set()
    await resolving
    await retrying
    with pytest.raises(ValueError):
        await competing
    assert calls == [("toggl", "研究 / vlm", 1500)]
    assert len(assistant.hub.recorded) == 1 and module.entries.entry(entry.id).sent


async def test_toggl_failure_holds_notion_until_retry_succeeds(env, monkeypatch, caplog):
    assistant, module, _ = env
    use_toggl(monkeypatch, module, RecordingToggl([], fail=TogglError("POST: 503")))
    entry = await measure(module, "C1", "vlm")
    assert (entry.toggl_state, entry.notion_state) == ("pending", "pending")
    assert assistant.hub.recorded == [] and "503" in caplog.text
    assert assistant.store.module_record("time", "entry", entry.id)["expires_at"] is None
    use_toggl(monkeypatch, module, RecordingToggl([]))
    await module._look_around()
    assert module.entries.entry(entry.id).sent


async def test_notion_retries_without_resending_toggl(env, monkeypatch):
    assistant, module, slack = env
    hub, assistant.hub = assistant.hub, None
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls))
    waiting = await measure(module, "C1", "vlm")
    assistant.hub = hub
    original = hub.record_time

    def broken(*args):
        raise NotionError("503")

    monkeypatch.setattr(hub, "record_time", broken)
    await module._look_around()
    assert module.entries.entry(waiting.id).notion_state == "pending"
    monkeypatch.setattr(hub, "record_time", original)
    await module._look_around()
    assert module.entries.entry(waiting.id).sent and len(calls) == 1 and slack.posted() == []


async def test_all_domains_deliver_and_successful_entries_expire(env, monkeypatch):
    assistant, module, _ = env
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls))
    research = await measure(module, "C1", "vlm")
    course = await measure(module, "C2", "線形代数", "course")
    work = await measure(module, "C3", "定例", "work")
    assert calls == [("toggl", "研究 / vlm", 1500), ("toggl", "大学 / 線形代数", 1500),
                     ("toggl", "仕事 / 定例", 1500)]
    assert assistant.hub.recorded == [(research.id, "research", "vlm", 25, "Slack"),
                                     (course.id, "course", "線形代数", 25, "Slack"),
                                     (work.id, "work", "定例", 25, "Slack")]
    row = assistant.store.module_record("time", "entry", research.id)
    assert row["expires_at"] == pytest.approx(time.time() + entries.KEEP_DAYS * 86400, abs=60)


async def test_toggl_delivery_interrupted_mid_send_waits_for_confirmation(env, monkeypatch):
    _, module, _ = env
    use_toggl(monkeypatch, module, RecordingToggl([]))
    started, _ = module.entries.start("UME", "research", "C1", "vlm")
    stopped = module.entries.stop("UME")
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(func, *args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(module.core, "to_thread", delayed)
    sending = asyncio.create_task(module._sync(stopped))
    await asyncio.wait_for(entered.wait(), 2)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    await module._look_around()
    assert module.entries.entry(started.id).toggl_state == "needs_review"
    assert (await timer(module))["needs_review"][0]["id"] == started.id


async def test_background_retry_runs_only_one_sweep_at_a_time(env, monkeypatch):
    assistant, module, _ = env
    entry = await review_entry(module)
    module.entries.set_delivery(entry.id, toggl_state="pending")
    gate = asyncio.Event()
    runs = []

    async def slow(entry):
        runs.append(entry.id)
        await gate.wait()

    monkeypatch.setattr(module, "_sync", slow)
    await module.tick(datetime.now())
    await asyncio.sleep(0)
    await module.tick(datetime.now())
    await asyncio.sleep(0)
    assert runs == [entry.id]
    gate.set()
    await settle(assistant)


def test_the_toggl_import_runs_at_22_by_default():
    assert [(s.name, s.default) for s in modules.builtin()["time"].schedules] == [("toggl_import", "22:00")]


class FakeToggl:
    def __init__(self, entries_):
        self._entries = entries_
        self.asked = []

    def entries(self, since, until):
        self.asked.append((since, until))
        return self._entries


def toggl_entry(start, duration, project="研究/amr-query", **extra):
    """Toggl 2.0 の time-entries が返す1件（使う項目だけ）。"""
    e = {"start": start, "duration": duration, "type": "activity",
         "project": {"id": 7, "name": project} if project else None}
    return e | extra


class ImportHub:
    def __init__(self, known=()):
        self.known = set(known)
        self.recorded = []

    def time_ids_since(self, since):
        return set(self.known)

    def record_time(self, entry_id, domain, label, started_at, minutes, memo="", slack_url="", source="Slack"):
        self.recorded.append({"id": entry_id, "domain": domain, "label": label, "started_at": started_at,
                              "minutes": minutes, "memo": memo, "source": source})


def test_import_adds_only_marked_entries_measured_outside_slack():
    """Toggl のアプリで直接測った分だけを「toggl:<id>」で入れる。Slack から送った分と、入れ済みの分は飛ばす。"""
    slack_start = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    toggl = FakeToggl([
        # Slack から送った記録（Toggl 側の開始が数秒ずれても同じとみなす）
        toggl_entry((slack_start + timedelta(seconds=20)).isoformat(), 1500, project="研究 / vlm", id=1),
        # 開始が同じでも、長さが1分より大きくずれていれば別の記録
        toggl_entry(slack_start.isoformat(), 1500 + 120, project="研究/vlm", id=8),
        toggl_entry("2026-09-21T13:00:00Z", 3000, project="大学/データベース", id=2, description="過去問"),
        toggl_entry("2026-09-21T15:00:00Z", 600, project="アルバイト", id=3),        # 印がない
        toggl_entry("2026-09-21T16:00:00Z", -1, project="研究/vlm", id=4),         # 計測中
        toggl_entry("2026-09-21T17:00:00Z", 900, project="仕事/定例", id=5),        # 入れ済み
        toggl_entry("2026-09-21T18:00:00Z", 900, project="研究/vlm", id=6, type="break"),
        toggl_entry("2026-09-21T19:00:00Z", 30, project="仕事/定例", id=7, description="仕事/定例"),
    ])
    hub = ImportHub(known={"toggl:5"})

    result = importer.import_toggl(toggl, hub, [(slack_start.timestamp(), 1500.0)], date(2026, 9, 15),
                                   date(2026, 9, 21))

    assert toggl.asked == [(date(2026, 9, 15), date(2026, 9, 21))]
    assert [r["id"] for r in hub.recorded] == ["toggl:8", "toggl:2", "toggl:7"]
    first = hub.recorded[1]
    assert (first["domain"], first["label"], first["minutes"], first["memo"], first["source"]) == (
        "大学", "データベース", 50, "過去問", "Toggl")
    assert datetime.fromisoformat(first["started_at"]) == datetime(2026, 9, 21, 13, 0, tzinfo=UTC)
    # 説明がプロジェクト名と同じならメモにしない。1分に満たなくても1分として残す
    assert (hub.recorded[2]["memo"], hub.recorded[2]["minutes"]) == ("", 1)
    assert result == {"status": "done", "imported": 3, "own": 1, "known": 1, "unmarked": 1}


def test_split_project_needs_a_known_mark():
    assert importer.split_project("研究/amr-query") == ("研究", "amr-query")
    assert importer.split_project("大学/マルチメディア工学A") == ("大学", "マルチメディア工学A")
    assert importer.split_project("amr-query") is None       # 印がない
    assert importer.split_project("趣味/写真") is None        # 知らない印
    assert importer.split_project("研究/") is None            # 名前がない


async def test_the_schedule_imports_toggl_and_skips_slack_entries(env, monkeypatch):
    assistant, module, slack = env
    started = time.time() - 3600
    entry, _ = module.entries.start("UME", "research", "C1", "vlm", started_at=started)
    module.entries.stop("UME", ended_at=started + 1500)
    iso = datetime.fromtimestamp(started).astimezone().isoformat()
    use_toggl(monkeypatch, module, FakeToggl([
        {"id": 1, "start": iso, "duration": 1500, "project": {"name": "研究 / vlm"}},
        {"id": 2, "start": "2026-09-17T10:00:00+09:00", "duration": 600, "project": {"name": "仕事/定例"}}]))

    detail = await module.run_schedule("toggl_import", date.today().isoformat())

    assert assistant.hub.recorded == [("toggl:2", "仕事", "定例", 10, "Toggl")]
    assert (detail["imported"], detail["own"]) == (1, 1)


async def test_the_schedule_waits_for_the_time_db_and_reports_toggl_failures(env, monkeypatch):
    assistant, module, slack = env
    monkeypatch.setattr(sys.modules[type(module).__module__], "load_toggl",
                        lambda: pytest.fail("時間記録が無いのに Toggl を読んだ"))
    assistant.hub.has_time_db = False
    assert await module.run_schedule("toggl_import", "2026-09-18") == {"status": "skipped", "reason": "no_hub"}
    hub, assistant.hub = assistant.hub, None
    assert await module.run_schedule("toggl_import", "2026-09-18") == {"status": "skipped", "reason": "no_hub"}

    class Broken:
        def entries(self, since, until):
            raise TogglError("GET /time-entries: 503")

    hub.has_time_db, assistant.hub = True, hub
    use_toggl(monkeypatch, module, Broken())
    detail = await module.run_schedule("toggl_import", "2026-09-18")
    assert detail["status"] == "error" and "503" in detail["error"]


# Daily と振り返りの材料

async def test_the_material_shows_this_weeks_time(env):
    assistant, module, slack = env
    assistant.hub.minutes = {"研究": 90, "大学": 30}
    lines = await module.material(datetime(2026, 9, 18, 8, 0).timestamp())
    assert lines[1] == "## 時間（今週）"
    assert "- 人: 合計 2.0 時間（研究 1.5 時間、大学 0.5 時間）" in lines
    assert "- 時間記録（週ごとのグラフ）: https://www.notion.so/timedb" in lines

    assistant.hub.minutes = {}
    assert any("まだ記録がない" in line and "`研究/`・`大学/`・`仕事/`" in line
               for line in await module.material(time.time()))
    assistant.hub = None
    assert (await module.material(time.time()))[-1] == "- 人: 共通 Notion ホームが使えないので分からない"
