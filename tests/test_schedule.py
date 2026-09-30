import asyncio
import json
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack, make_theme

from kei_agent import morning, runner, themes
from kei_agent import schedule as schedule_module
from kei_agent.assistant import Assistant
from kei_agent.calendar_sync import SyncReport
from kei_agent.jobs import JobManager
from kei_agent.notion_store import Note
from kei_agent.schedule import Scheduler, due_day


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "01_overview", "C9": "00_kei-agent"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack, claude


async def test_scheduler_says_once_when_offline_and_once_when_back(env, monkeypatch, caplog):
    """ネットにつながらない間は、毎分の長いエラーの代わりに、始めと戻りを1行ずつ。ほかの失敗は今までどおり。"""
    scheduler, *_ = env
    failures = [ConnectionError("名前を引けない"), OSError("Cannot connect")]
    outcomes = [failures[0], failures[0], None, RuntimeError("こわれた")]

    async def tick(now):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(scheduler, "tick", tick)
    caplog.set_level("INFO", logger="kei_agent.schedule")
    for _ in range(4):
        await scheduler.safe_tick(datetime(2026, 9, 26, 12, 0))

    records = [(r.levelname, r.getMessage().split("（")[0]) for r in caplog.records]
    assert records == [("WARNING", "ネットにつながらないので、定期処理はつながるまで待ちます: ConnectionError: 名前を引けない"),
                       ("INFO", "ネットにつながったので、定期処理を続けます"),
                       ("ERROR", "定期処理に失敗しました")]
    assert caplog.records[-1].exc_info is not None                 # ほかの失敗は、原因をたどれるように残す
    assert schedule_module.offline(TimeoutError()) and not schedule_module.offline(failures[1])


async def test_hub_calendar_copies_all_future_assignments_without_asking_work(env, monkeypatch):
    """課題はこれからの全部を写す（大学のモジュールの見回り）。会議は朝の Daily で書くので、仕事の担当には聞かない。"""
    from kei_agent import api

    scheduler, assistant, *_ = env
    seen = []
    agent = FakeCourseAgent([], assignments=[{"id": "assignment-1", "title": "課題", "due": "2027-02-01",
                                              "status": "未着手", "url": "https://notion.so/assignment-1"}])
    assistant.agents["course"] = agent

    async def no_work(*args, **kwargs):
        raise AssertionError("予定カレンダーのために仕事の担当を動かさない")

    def record_sync(hub, snapshot, checked_at, days):
        seen.append((snapshot.source, [i.source_id for i in snapshot.items], days))
        return SyncReport(1, 0, 0)

    monkeypatch.setattr(assistant.modules["work"], "agenda", no_work)
    monkeypatch.setattr(api, "sync_calendar", record_sync)

    await assistant.modules["course"].sync_calendar(datetime(2026, 9, 26, 8, 5))

    assert agent.asked == [("list-calendar-assignments", {"days": 400, "provider": "claude"})]
    assert seen == [("課題", ["assignment-1"], 400)]
    record = assistant.modules["course"].core.records.get("calendar", "synced")
    assert record == {"day": "2026-09-26", "created": 1, "updated": 0, "stale": 0}


async def test_hub_calendar_sync_without_hub_does_not_call_agents(env):
    scheduler, assistant, *_ = env
    assistant.hub = None
    agent = FakeCourseAgent([])
    assistant.agents["course"] = agent
    await assistant.modules["course"].sync_calendar(datetime(2026, 9, 24, 9, 0))
    assert agent.asked == []


async def test_hub_schema_failure_disables_only_hub(env, monkeypatch):
    _, assistant, *_ = env
    notices = []

    async def notice(text):
        notices.append(text)

    monkeypatch.setattr(assistant, "notify_trouble", notice)
    monkeypatch.setattr(assistant.hub, "schema_problems", lambda: ["日別記録の列がありません"])
    assert await assistant.check_hub_schema() == ["日別記録の列がありません"]
    assert assistant.hub is None
    assert assistant.notion is not None
    assert notices


async def test_missing_time_db_is_reported_but_keeps_the_hub(env, monkeypatch):
    """時間記録がまだ無いだけなら、日別記録は使い続け、作り方を一度だけ知らせる。"""
    _, assistant, *_ = env
    notices = []

    async def notice(text):
        notices.append(text)

    monkeypatch.setattr(assistant, "notify_trouble", notice)
    hub = assistant.hub
    hub.has_time_db = False
    assert await assistant.check_hub_schema() == []
    assert assistant.hub is hub
    notice_text, = notices
    assert "時間記録" in notice_text and "kei-agent-hub-setup --apply" in notice_text


async def test_hub_calendar_runs_without_daily_and_retries_after_failed_hour(env):
    """全部を読めなかった日は、1時間おきに写し直す（Daily が動いていなくても、見回りが写す）。"""
    scheduler, assistant, *_ = env
    agent = FakeCourseAgent([])
    agent.incomplete = True
    assistant.agents["course"] = agent
    module = assistant.modules["course"]
    for at in ("2026-09-18T08:05", "2026-09-18T08:06", "2026-09-18T09:06"):
        await module.sync_calendar(datetime.fromisoformat(at))
    assert [skill for skill, _ in agent.asked] == ["list-calendar-assignments", "list-calendar-assignments"]


async def test_hub_calendar_is_done_for_the_day_once_assignments_are_copied(env):
    scheduler, assistant, *_ = env
    agent = FakeCourseAgent([], assignments=[{"id": "a", "title": "課題", "due": "2026-10-01", "status": "未着手"}])
    assistant.agents["course"] = agent
    module = assistant.modules["course"]
    await module.sync_calendar(datetime.fromisoformat("2026-09-18T08:05"))
    await module.sync_calendar(datetime.fromisoformat("2026-09-18T10:06"))
    assert [skill for skill, _ in agent.asked] == ["list-calendar-assignments"]


# 時刻

@pytest.mark.parametrize("now, hhmm, hours, expected", [
    ("2026-09-18 08:00", "08:00", 3, "2026-09-18"),
    ("2026-09-18 10:59", "08:00", 3, "2026-09-18"),
    ("2026-09-18 11:01", "08:00", 3, None),
    ("2026-09-18 07:59", "08:00", 3, None),
    ("2026-09-18 00:30", "23:30", 3, "2026-09-17"),   # 日をまたいで追いつく
    ("2026-09-18 09:00", "01:30", 12, "2026-09-18"),  # 夜間の Task は朝に起きても実行する
    ("2026-09-18 09:00", "", 3, None),
])
def test_due_day(now, hhmm, hours, expected):
    assert due_day(datetime.fromisoformat(now), hhmm, hours) == expected


def test_search_keywords_ignores_template_comment(config):
    ws = make_theme(config, keywords=None)
    assert themes.search_keywords(ws.cwd / "CLAUDE.md") == []
    ws = make_theme(config, name="other", keywords=["vlm counting", "object counting benchmark"])
    assert themes.search_keywords(ws.cwd / "CLAUDE.md") == ["vlm counting", "object counting benchmark"]


async def test_tick_runs_each_task_once_per_day(env, monkeypatch):
    scheduler, *_ = env
    ran = []

    async def fake_run(name, day, record=True):
        ran.append((name, day))
        scheduler.store.record_schedule(name, day, {"status": "done"})
        return {}

    monkeypatch.setattr(scheduler, "run_task", fake_run)
    await scheduler.tick(datetime.fromisoformat("2026-09-18 08:05"))
    await scheduler.tick(datetime.fromisoformat("2026-09-18 08:06"))
    # 01:30 の夜間、07:00 の先行研究と読みもの、08:00 の Daily が1回ずつ。21:00 はまだ
    assert ran == [("night", "2026-09-18"), ("literature", "2026-09-18"), ("reading", "2026-09-18"),
                   ("daily", "2026-09-18")]

    await scheduler.tick(datetime.fromisoformat("2026-09-18 22:10"))
    assert ran[-2:] == [("review", "2026-09-18"), ("maintenance", "2026-09-18")]


async def test_tick_follows_times_changed_in_slack(env, monkeypatch):
    """App Home で変えた時刻は、再起動なしで次の tick から効く。止めた処理は動かさない。"""
    scheduler, *_ = env
    from kei_agent import settings
    ran = []

    async def fake_run(name, day, record=True):
        ran.append(name)
        scheduler.store.record_schedule(name, day, {"status": "done"})
        return {}

    monkeypatch.setattr(scheduler, "run_task", fake_run)
    settings.set_schedule(scheduler.store, "daily", "07:30", True)
    settings.set_schedule(scheduler.store, "literature", "07:00", False)
    await scheduler.tick(datetime.fromisoformat("2026-09-18 07:35"))
    assert "daily" in ran and "literature" not in ran


# 🌙 の夜間 Task

MOON = {"reaction": "crescent_moon", "user": "UME", "item_user": "UME",
        "item": {"type": "message", "channel": "C1", "ts": "1789636798.229039"}}


async def test_moon_reaction_creates_notion_task_and_removal_cancels(env):
    scheduler, assistant, slack, claude = env
    slack.replies = [{"ts": "1789636798.229039", "user": "UME", "text": "条件Cも回して\n試行は3回"}]

    await assistant.on_reaction_added(MOON)

    task, = assistant.notion.tasks.values()
    assert (task.title, task.status, task.theme_names) == ("条件Cも回して", "今夜やる", ["vlm"])
    assert task.slack_url == "https://example.slack.com/archives/C1/p1789636798229039"
    assert "> 試行は3回" in assistant.notion.bodies[task.id]
    assert slack.texts()[-1].startswith("🌙 今夜の Task にしたよ")

    await assistant.on_reaction_removed(MOON)
    assert task.status == "未着手"

    await assistant.on_reaction_added(MOON)  # もう一度つけても Task は増えない
    assert len(assistant.notion.tasks) == 1 and task.status == "今夜やる"


async def test_moon_reaction_ignored_for_others_and_non_theme(env):
    scheduler, assistant, *_ = env
    await assistant.on_reaction_added({**MOON, "user": "USOMEONE", "item_user": "USOMEONE"})
    await assistant.on_reaction_added({**MOON, "item_user": "UOTHER"})
    await assistant.on_reaction_added({**MOON, "reaction": "eyes"})
    await assistant.on_reaction_added({**MOON, "item": {"type": "message", "channel": "C9", "ts": "60.1"}})
    assert assistant.notion.tasks == {}


async def test_moon_reaction_reports_notion_failure(env):
    scheduler, assistant, slack, claude = env
    assistant.notion.fail = True
    await assistant.on_reaction_added(MOON)
    posted = slack.posted()
    assert posted[0]["text"] == "⚠️ Notion に Task を作れなかったよ"
    assert posted[1]["channel"] == "C9" and "確認が必要な問題" in posted[1]["text"]


async def test_night_runs_slack_task_in_its_thread(env, config):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    task = assistant.notion.add_task("条件Cも回して", "vlm",
                                     slack_url="https://example.slack.com/archives/C1/p1789636798229039",
                                     body="試行は3回")
    slack.replies = [{"ts": "1789636798.229039", "thread_ts": "1789636700.000100", "user": "UME",
                      "text": "条件Cも回して"}]
    claude.behaviors = [{"text": "## 結果\n条件Cは 71% でした"}]

    detail = await scheduler.run_night("2026-09-18")

    call, = claude.calls
    assert call["thread_ts"] == "1789636700.000100" and call["cwd"] == config.research_root / "vlm"
    assert "[🌙 夜間の Task]" in call["prompt"] and "試行は3回" in call["prompt"] and task.url in call["prompt"]
    assert task.status == "完了" and assistant.notion.results[task.id] == "結果 / 条件Cは 71% でした"
    assert ("reactions_add", {"channel": "C1", "timestamp": "1789636798.229039", "name": "white_check_mark"}) in slack.calls
    assert detail["tasks"][0]["status"] == "完了" and detail["remaining"] == 0


async def test_night_runs_notion_task_in_new_thread(env, config):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    task = assistant.notion.add_task("先行研究を追加で探す", "vlm", body="2024年以降に絞る")

    await scheduler.run_night("2026-09-18")

    header = slack.posted()[0]
    assert header == {"channel": "C1", "text": "🌙 Task: 先行研究を追加で探す"}
    assert claude.calls[0]["thread_ts"] == "1001.000"
    assert task.slack_url.startswith("https://example.slack.com/archives/C1/p")
    assert task.status == "完了"


async def test_night_marks_awaiting(env, config):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    no_theme = assistant.notion.add_task("テーマなし")
    asks = assistant.notion.add_task("判断が要る", "vlm")
    claude.behaviors = [{"text": "途中まで\n❓ 確認: 条件Bも含めますか？"}]

    await scheduler.run_night("2026-09-18")

    assert no_theme.status == "確認待ち" and assistant.notion.results[no_theme.id] == "テーマを設定してください"
    assert asks.status == "確認待ち" and len(claude.calls) == 1


async def test_night_respects_limit(env, config):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    for i in range(7):
        assistant.notion.add_task(f"task {i}", "vlm")
    detail = await scheduler.run_night("2026-09-18")
    assert len(claude.calls) == 5 and detail["remaining"] == 2


async def test_night_skips_when_notion_is_down(env, config):
    scheduler, assistant, slack, claude = env
    assistant.notion.fail = True
    detail = await scheduler.run_night("2026-09-18")
    assert detail["status"] == "error" and claude.calls == []
    assert slack.posted()[-1]["channel"] == "C9" and "確認が必要な問題" in slack.posted()[-1]["text"]


async def test_member_joined_registers_theme_in_notion(env, config):
    """まだフォルダの無いテーマは、置き場所を聞いてから作る。既定の場所を選ぶと、フォルダを作って研究ホームに登録する。"""
    scheduler, assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
    assert assistant.notion.themes == {} and not (config.research_root / "vlm").exists()
    choice, = [kw for kw in slack.posted() if kw.get("blocks")]
    default = next(el for b in choice["blocks"] if b["type"] == "actions" for el in b["elements"]
                   if el["action_id"] == "kei_agent_theme_place_default")
    await assistant.on_theme_place_action({"user": {"id": "UME"}, "actions": [default],
                                           "container": {"channel_id": "C1", "message_ts": "5.5"}})
    assert assistant.notion.themes == {"vlm": "https://example.slack.com/archives/C1"}
    assert (config.research_root / "vlm" / "CLAUDE.md").exists()


# 返事待ちへの声かけ

async def test_awaiting_marker_nudges_once_and_clears_on_reply(env, config, store, monkeypatch):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": "途中まで進めました\n❓ 確認: 条件Bも含めますか？"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 進めて"})
    while assistant.tasks:
        import asyncio
        await asyncio.gather(*list(assistant.tasks))
    assert store.get_thread("C1", "10.1")["awaiting_since"] is not None

    await scheduler.nudge_stale_threads()
    assert not any("⏰" in t for t in slack.texts())  # まだ24時間たっていない

    store.conn.execute("UPDATE threads SET awaiting_since = ?", (time.time() - 25 * 3600,))
    await scheduler.nudge_stale_threads()
    await scheduler.nudge_stale_threads()
    assert sum("⏰" in t for t in slack.texts()) == 1

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.9", "thread_ts": "10.1", "text": "含めて"})
    assert store.get_thread("C1", "10.1")["awaiting_since"] is None


async def test_maintenance_forgets_awaits_that_stayed_unanswered(env, config, store):
    """声をかけても返事がないまま倍の時間がたった返事待ちは、保守で閉じる。

    閉じないと、App Home の「いま動いているもの」に何日も居座り続ける。
    """
    scheduler, assistant, slack, claude = env
    store.upsert_thread("C1", "10.1", "amr-query", "s1")
    store.set_awaiting("C1", "10.1", True)

    store.conn.execute("UPDATE threads SET awaiting_since = ?, nudged = 1", (time.time() - 30 * 3600,))
    detail = await scheduler.run_maintenance("2026-09-18")
    assert detail["awaits"] == 0                      # 声かけから 24 時間はまだ待つ
    assert store.threads_awaiting()

    store.conn.execute("UPDATE threads SET awaiting_since = ?, nudged = 1", (time.time() - 49 * 3600,))
    detail = await scheduler.run_maintenance("2026-09-18")
    assert detail["awaits"] == 1
    assert store.threads_awaiting() == []


async def test_maintenance_reports_backup_failure(env, config):
    scheduler, assistant, slack, claude = env
    config.research_root.mkdir(parents=True, exist_ok=True)  # Git のリポジトリではない
    detail = await scheduler.run_maintenance("2026-09-18")
    assert detail["status"] == "error" and detail["removed"] == {"sessions": 0, "thread_logs": 0}
    assert slack.posted()[-1]["channel"] == "C9" and "確認が必要な問題" in slack.posted()[-1]["text"]


async def test_night_task_recovers_from_unexpected_error(env, config, monkeypatch):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    task = assistant.notion.add_task("Slack が落ちている", "vlm")

    async def broken_post(**kw):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(slack, "chat_postMessage", broken_post)
    detail = await scheduler.run_night("2026-09-18")

    assert task.status == "確認待ち" and "slack is down" in assistant.notion.results[task.id]
    assert detail["tasks"][0]["status"] == "error"


async def test_night_task_from_other_channel_runs_in_theme_channel(env, config):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    assistant.notion.add_task("別のチャンネルで作った", "vlm",
                              slack_url="https://example.slack.com/archives/C9/p1789636798229039")
    slack.replies = [{"ts": "1789636798.229039", "user": "UME", "text": "別のチャンネルで作った"}]

    await scheduler.run_night("2026-09-18")

    assert slack.posted()[0] == {"channel": "C1", "text": "🌙 Task: 別のチャンネルで作った"}
    assert claude.calls[0]["cwd"] == config.research_root / "vlm"


async def test_nudge_failure_is_not_retried_every_minute(env, store, monkeypatch):
    scheduler, assistant, slack, claude = env
    store.upsert_thread("C1", "10.1", "vlm", "s")
    store.set_awaiting("C1", "10.1", True)
    store.conn.execute("UPDATE threads SET awaiting_since = ?", (time.time() - 30 * 3600,))
    calls = []

    async def archived(**kw):
        calls.append(kw)
        raise RuntimeError("is_archived")

    monkeypatch.setattr(slack, "chat_postMessage", archived)
    await scheduler.nudge_stale_threads()
    await scheduler.nudge_stale_threads()
    assert len(calls) == 1


def test_interrupted_schedule_runs_again(store):
    """途中で Kei Agent が止まると「実行中」の記録が残る。そのままだと、その日は二度と動かない。"""
    store.record_schedule("daily", "2026-09-19", {"status": "running"})
    assert store.schedule_ran("daily", "2026-09-19")

    assert store.mark_interrupted_schedules() == [("daily", "2026-09-19")]
    assert not store.schedule_ran("daily", "2026-09-19")

    store.record_schedule("daily", "2026-09-19", {"status": "posted"})
    assert store.mark_interrupted_schedules() == []
    assert store.schedule_ran("daily", "2026-09-19")


async def test_theme_is_registered_in_notion_on_first_use(env, config):
    """招待のイベントを取りこぼしても、初めて使うときに Notion のテーマの行ができる。"""
    scheduler, assistant, slack, claude = env
    make_theme(config, "vlm")
    assert assistant.notion.themes == {}

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks))

    assert "vlm" in assistant.notion.themes


async def test_moon_removed_from_someone_elses_message_is_ignored(env):
    """🌙 をつけるときと同じく、外すときも自分のメッセージだけを見る。"""
    scheduler, assistant, slack, claude = env
    assistant.notion.add_task("誰かの Task", "vlm", slack_url="https://example.slack.com/archives/C1/p101")

    await assistant.on_reaction_removed({
        "user": "UME", "item_user": "USOMEONE", "reaction": "crescent_moon",
        "item": {"type": "message", "channel": "C1", "ts": "10.1"},
    })

    assert [t.status for t in assistant.notion.tasks.values()] == ["今夜やる"]


# 契約の上限（Claude AI usage limit）


async def test_limited_schedule_is_deferred_only_once_per_day(env):
    scheduler, _, *_ = env
    scheduler.store.set_limit_until("claude", time.time() + 3600)
    now = datetime.fromisoformat("2026-09-18 08:05")
    await scheduler.tick(now)
    first = scheduler.store.pending_deferred("schedule")
    await scheduler.tick(now)
    assert scheduler.store.pending_deferred("schedule") == first


async def test_tick_waits_while_the_usage_limit_is_on(env, monkeypatch):
    scheduler, assistant, *_ = env
    ran = []
    async def record(name, day, record=True):
        ran.append(name)
    monkeypatch.setattr(scheduler, "run_task", record)
    scheduler.store.set_limit_until("claude", time.time() + 3600)

    await scheduler.tick(datetime.fromisoformat("2026-09-18 08:05"))
    assert ran == []  # 08:05 時点では保守はまだ期日ではない


async def test_a_task_stopped_by_the_limit_runs_again_after_it_resets(env, monkeypatch):
    """上限で止まった処理は、その日の分としては記録せず、明けてからやり直す。"""
    scheduler, assistant, *_ = env
    reset = time.time() + 3600
    ran = []

    async def hits_the_limit(name, day, record=True):
        ran.append((name, day))
        scheduler.store.set_limit_until("claude", reset)
        return {"status": "error"}

    monkeypatch.setattr(scheduler, "run_task", hits_the_limit)
    await scheduler.tick(datetime.fromisoformat("2026-09-18 08:05"))

    assert ran == [("night", "2026-09-18")]                         # 1つ目で上限に当たって止まる
    assert not scheduler.store.schedule_ran("night", "2026-09-18")  # その日の分としては記録しない
    assert scheduler.store.due_deferred("schedule", reset + 120)    # 明けたらやり直す

    done = []

    async def works(name, day, record=True):
        done.append((name, day))
        scheduler.store.record_schedule(name, day, {"status": "done"})

    monkeypatch.setattr(scheduler, "run_task", works)
    scheduler.store.set_limit_until("claude", 0)
    await scheduler.catch_up_deferred(reset + 120)
    assert done == [("night", "2026-09-18"), ("literature", "2026-09-18"), ("reading", "2026-09-18"),
                    ("daily", "2026-09-18")]
    assert scheduler.store.due_deferred("schedule", reset + 200) == []


# 授業の締切（大学エージェント）


class FakeCourseAgent:
    """大学エージェントの代わり。list-due は JSON を返す。"""
    base_url = "http://127.0.0.1:8787"

    def __init__(self, items, classes=None, synced=None, assignments=None):
        self.items = items
        self.classes = classes or []
        self.synced = synced or {}
        self.assignments = assignments or []
        self.asked = []

    async def stream(self, skill, text="", params=None, on_progress=None):
        from kei_agent import a2a
        from kei_agent.dates import weekday
        params = {**(json.loads(text) if text.startswith("{") else {}), **(params or {})}
        self.asked.append((skill, params))
        data = {}
        if skill == "list-due":
            data = {"days": params.get("days"), "items": self.items}
        elif skill == "list-classes":
            wanted = params.get("weekday")
            data = {"items": [item for item in self.classes if not wanted or not item.get("start")
                              or weekday(date.fromisoformat(item["start"][:10])) == wanted]}
        elif skill == "sync-assignments":
            data = dict(self.synced)
        elif skill == "list-calendar-assignments":
            data = {"complete": not getattr(self, "incomplete", False), "items": self.assignments}
        # 返事は全エージェント共通の封筒
        envelope = {"ok": True, "text": f"{skill} をやったよ", "data": data,
                    "limit_reset_at": None, "cost_usd": None}
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps(envelope, ensure_ascii=False))


class FakeWorkAgent:
    """仕事の担当の代わり（Outlook の予定）。仕事のモジュールは、日数を本文の JSON で頼む。"""
    base_url = "http://127.0.0.1:8789"

    def __init__(self, items):
        self.items = items
        self.asked = []

    async def stream(self, skill, text="", params=None, on_progress=None):
        from kei_agent import a2a
        self.asked.append((skill, json.loads(text)))
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps(
            {"ok": True, "text": "予定", "data": {"items": self.items},
             "limit_reset_at": None, "cost_usd": None}))


def due_item(at, title="第3回レポート の 提出期限", course="データベース", uid="1@moodle"):
    return {"id": uid, "at": at, "course": course, "title": title, "url": "https://moodle/x"}


async def test_morning_text_puts_everything_on_one_timeline(env):
    """授業・会議・締切を、研究／授業／仕事で分けずに時刻の早い順で1本にする。"""
    scheduler, assistant, slack, _ = env
    now = datetime.now().replace(hour=7, minute=30, second=0, microsecond=0)
    today = now.date().isoformat()
    assistant.agents["course"] = FakeCourseAgent(
        [due_item(f"{today}T17:00:00+09:00", "履修申請フォーム", "プロジェクト研究B")],
        classes=[{"subject": "データベース", "start": f"{today}T10:40", "end": f"{today}T12:20"}])
    assistant.agents["work"] = FakeWorkAgent(
        [{"subject": "朝会", "start": f"{today}T10:00", "end": f"{today}T10:15", "location": "Zoom"}])

    text, detail, _ = await scheduler.morning_text(now)

    lines = text.splitlines()
    # 帯と空き時間はやめた（2026-09-22）。時刻の一覧だけにする
    assert lines[0].startswith("☀️")
    assert "10:00–10:15` 💼 朝会（Zoom）" in lines[1]
    assert "10:40–12:20` 🎓 データベース" in lines[2]
    assert "17:00" in lines[3] and "⏰ 締切: プロジェクト研究B 履修申請フォーム" in lines[3]
    assert "空き:" not in text and "9時 " not in text
    # 読んだ会議は予定カレンダーにも書く（AI をもう一度動かさない）。仕事のモジュールの予定（agenda）から
    assert detail == {"classes": 1, "dues": 1, "events": 1,
                      "agenda": {"synced": {"Outlook": {"created": 1, "updated": 0, "stale": 0}}, "unread": []}}
    assert [(row["出典"], row["名前"]) for row in assistant.hub.calendar] == [("Outlook", "朝会")]
    assert assistant.agents["work"].asked == [("list-events", {"days": schedule_module.VOICE_DAYS})]


# 1日の帯と空き時間（morning.py）

def _entry(at, end=None, icon=morning.CLASS, text="授業"):
    day = date(2026, 9, 21)
    return morning.Entry(datetime.combine(day, dtime(*at)), icon, text,
                         datetime.combine(day, dtime(*end)) if end else None)


async def test_morning_text_works_without_the_agents(env):
    """エージェントがいないときは、黙って「予定なし」にする（朝の通知は止めない）。"""
    scheduler, *_ = env
    text, detail, _ = await scheduler.morning_text(datetime.now())
    assert morning.NOTHING in text and detail == {"classes": 0, "dues": 0, "events": 0}


async def test_the_morning_list_does_not_repeat_as_a_reminder(env):
    """朝のまとめに出した締切は、そのあとのまとめ知らせ（締切の課題）で繰り返さない。"""
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    soon = (datetime.now() + timedelta(hours=5)).astimezone().isoformat()
    assistant.agents["course"] = FakeCourseAgent([due_item(soon)], assignments=[
        {"id": "notion-1", "title": "第3回レポート", "due": soon, "status": "未着手",
         "url": "https://notion.so/x", "course": "データベース", "moodle": "https://moodle/x",
         "moodle_id": "1@moodle"}])

    _, _, notices = await scheduler.morning_text(datetime.now())
    assert notices and all(key.startswith("module.course.due:") for key in notices)
    for key in notices:
        scheduler.store.record_notice(key)
    before = len(slack.posted())
    await assistant.modules["course"].notify_due_soon(datetime.now())

    assert len(slack.posted()) == before


async def test_due_check_retries_immediately_after_the_agent_fails(env, monkeypatch):
    """一時的に一覧を取れなくても、1時間待たず次の tick で取り直す。"""
    from kei_agent.agents import Reply

    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    calls = 0

    async def ask_agent(skill, payload):
        nonlocal calls
        calls += 1
        return Reply.broken("だめ") if calls == 1 else Reply(ok=True, data={"items": []})

    module = assistant.modules["course"]
    monkeypatch.setattr(module.core, "ask_agent", ask_agent)
    now = datetime.now()

    await module.notify_due_soon(now)
    await module.notify_due_soon(now)

    assert calls == 2


# 振り返りの材料に、大学と仕事を足す（digest.py）

async def test_review_digest_gathers_all_three_domains(env, config, store):
    """振り返りの材料には、研究だけでなく大学と仕事も入れる。"""
    from kei_agent.digest import DigestBuilder

    scheduler, assistant, slack, _ = env
    now = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
    today, tomorrow = now.date().isoformat(), (now + timedelta(days=1)).date().isoformat()
    assistant.agents["course"] = FakeCourseAgent(
        [due_item(f"{today}T17:00:00", "履修申請フォーム", "プロジェクト研究B"),
         due_item(f"{tomorrow}T23:59:00", "第3回レポート", "データベース", "2@moodle")],
        classes=[{"subject": "マルチメディア工学A", "start": f"{tomorrow}T13:00", "end": f"{tomorrow}T14:40"}])
    assistant.agents["work"] = FakeWorkAgent(
        [{"subject": "定例MTG", "start": f"{today}T18:00", "end": f"{today}T19:00"},
         {"subject": "ゆうちょ様AML", "start": f"{tomorrow}T11:00", "end": f"{tomorrow}T13:00"}])

    text = await DigestBuilder(config, store, assistant).build(
        now.timestamp() - 86400, now.timestamp(), "振り返りの材料", set(), domains=True)

    # どちらもモジュールの予定（agenda）から。見出しはモジュールの表示名
    university = text.split("## 大学")[1].split("## ")[0]
    assert "今日が期限だったもの: プロジェクト研究B / 履修申請フォーム（17:00）" in university
    assert "第3回レポート" in university.split("残っている締切:")[1].splitlines()[0]
    assert "明日の予定: 13:00–14:40 マルチメディア工学A" in university
    work = text.split("## 仕事")[1].split("## ")[0]
    assert "今日あった予定: 18:00–19:00 定例MTG" in work
    assert "明日の予定: 11:00–13:00 ゆうちょ様AML" in work


async def test_daily_digest_does_not_ask_the_agents_again(env, config, store):
    """朝は、朝のまとめですでに聞いているので、材料づくりで二度聞かない（claude を無駄に動かさない）。"""
    from kei_agent.digest import DigestBuilder

    scheduler, assistant, slack, _ = env
    course_agent = FakeCourseAgent([])
    work_agent = FakeWorkAgent([])
    assistant.agents["course"], assistant.agents["work"] = course_agent, work_agent

    text = await DigestBuilder(config, store, assistant).build(
        time.time() - 86400, time.time(), "Daily の材料", set())

    assert "## 大学" not in text and "## 仕事" not in text
    assert course_agent.asked == [] and work_agent.asked == []


def test_the_voice_layer_gets_a_week_not_just_today():
    """声のレイヤは「明日の予定」「今週の予定」に答える。

    今日ぶんだけ渡していたせいで、明日を聞かれても今日の予定を答えていた（実測）。
    """
    from datetime import datetime

    classes = [{"start": "2026-09-21T10:40", "end": "2026-09-21T12:20", "subject": "データベース"},
               {"start": "2026-09-22T13:00", "end": "2026-09-22T14:40", "subject": "信号処理"},
               {"start": "2026-10-01T13:00", "end": "2026-10-01T14:40", "subject": "来月の授業"}]
    dues = [{"at": "2026-09-24T17:00", "course": "実験", "title": "第3回レポート"}]
    now = datetime(2026, 9, 21, 9, 0)

    today = morning.entries(classes, [], dues, now)
    assert [e.text for e in today] == ["データベース"]

    week = morning.upcoming(classes, [], dues, now, days=7)
    assert [e.text for e in week] == ["データベース", "信号処理", "締切: 実験 第3回レポート"]
    # 範囲の外は入らない
    assert "来月の授業" not in [e.text for e in week]


# 材料の中身（digest.py）

async def test_digest_reads_yesterday_review_and_week_time_from_the_hub(env, config, store):
    """前日の振り返りは日別記録から、今週の時間は時間記録（時間記録のモジュール）と runs から読む（手元のファイルは見ない）。"""
    from kei_agent.digest import DigestBuilder

    _, assistant, *_ = env
    now = datetime(2026, 9, 18, 8, 0)
    assistant.hub.reviews["2026-09-17"] = "**今日の成果**\n条件Bを回した\n\n### Slack に貼った結論\n順番が効く"
    # 前日の行は「前日の振り返り」に丸ごと入れるので、ノートとしては重ねない
    assistant.hub.edited = [Note("hub-2026-09-17", "Retro", "振り返り", "2026-09-17",
                                 "https://notion.example/hub/2026-09-17", "前日の行の全文")]
    assistant.hub.minutes = {"研究": 90, "大学": 30}
    run = store.start_run("C1", "1.1", "vlm", "message")
    store.end_run(run, False, None)
    start = datetime(2026, 9, 15, 10, 0).timestamp()
    store.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?", (start, start + 1800, run))
    store.conn.commit()

    text = await DigestBuilder(config, store, assistant).build(
        now.timestamp() - 86400, now.timestamp(), "Daily の材料", set())

    review = text.split("## 前日の振り返り")[1].split("\n## ")[0]
    assert "条件Bを回した" in review and "順番が効く" in review
    assert "前日の行の全文" not in text
    # 人の時間は時間記録のモジュールが足し、Kei Agent の稼働は本体が数える
    week = text.split("## 時間（今週）")[1].split("\n## ")[0]
    assert "合計 2.0 時間（研究 1.5 時間、大学 0.5 時間）" in week
    assert "https://www.notion.so/timedb" in week
    assert "- 合計 0.5 時間" in text.split("## Kei Agent の稼働（今週）")[1].split("\n## ")[0]


async def test_digest_is_capped_but_keeps_the_task_lists(env, config, store):
    """材料が長すぎるときは長い本文から削り、今日のタスクの元になる一覧は残す。"""
    from kei_agent import digest
    from kei_agent.digest import DigestBuilder

    _, assistant, *_ = env
    today = datetime.now().date()
    task = assistant.notion.add_task("今日の締切の Task", "vlm", status="未着手")
    task.due = today.isoformat()
    for i in range(40):
        assistant.notion.notes.append(Note(f"note-{i}", f"考察{i}", "考察", "2026-09-17",
                                           f"https://notion.example/note-{i}", "あ" * 2000))

    text = await DigestBuilder(config, store, assistant).build(
        time.time() - 86400, time.time(), "Daily の材料", set())

    assert len(text) <= digest.MAX_DIGEST_CHARS + 200
    assert "今日の締切の Task" in text
    assert digest.TRUNCATED in text


# Moodle の取り込みの知らせと、レトプラの締切


async def test_scheduled_sync_announces_new_and_changed_assignments(env):
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    assistant.agents["course"] = FakeCourseAgent([], synced={
        "added": ["10/26 00:00 情報 / Assignment A"], "updated": ["11/02 00:00 情報 / Assignment B"]})

    assert await assistant.modules["course"].sync_assignments() is True

    post = slack.posted()[-1]
    assert post["channel"] == "C7"
    assert post["text"] == ("📚 Moodle の課題（新着 1件・締切変更 1件）\n• 10/26 00:00 情報 / Assignment A\n"
                            "• :repeat: 11/02 00:00 情報 / Assignment B")


async def test_scheduled_sync_stays_quiet_without_changes(env):
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    assistant.agents["course"] = FakeCourseAgent([])
    before = len(slack.posted())

    assert await assistant.modules["course"].sync_assignments() is True
    assert len(slack.posted()) == before


# 締切3日前の未着手、うまくいかなかったこと、起動し直していない新しい版


async def test_unstarted_assignments_are_noticed_three_days_ahead_once(env):
    """3日以内で未着手の課題は「締切の課題」に1回だけまとめて出す。提出済みは出ない。"""
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    now = datetime(2026, 10, 23, 12, 0)
    assistant.agents["course"] = FakeCourseAgent([], assignments=[
        {"id": "a", "title": "Assignment A", "due": "2026-10-26T00:00:00.000+09:00", "status": "未着手",
         "url": "https://notion.so/a", "course": "データベース", "moodle": "https://moodle.example/a"},
        {"id": "b", "title": "もう出した", "due": "2026-10-25T12:00:00.000+09:00", "status": "提出済み"},
        {"id": "c", "title": "まだ先", "due": "2026-10-30T12:00:00.000+09:00", "status": "未着手"},
    ])

    module = assistant.modules["course"]
    await module.notify_due_soon(now)
    module._due_checked = 0.0
    await module.notify_due_soon(now)

    posts = [p["text"] for p in slack.posted() if p.get("text", "").startswith("締切の課題")]
    assert posts == ["締切の課題\n• あと2日：データベース／Assignment A（https://moodle.example/a）"]


async def test_the_morning_says_what_went_wrong_since_the_last_daily(env):
    scheduler, assistant, slack, _ = env
    store = scheduler.store
    now = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    store.record_schedule("daily", yesterday, {"status": "posted", "notion_url": None})
    store.record_schedule("review", yesterday, {"status": "error"})
    store.record_schedule("maintenance", yesterday, {"status": "done"})

    note = scheduler.failure_note(now, ["課題の取り込み"])

    assert note == "⚠️ うまくいかなかったこと: Daily（Notion に残せず）、Retro & Planning、課題の取り込み"
    store.record_schedule("review", yesterday, {"status": "posted", "notion_url": "https://notion.so/r"})
    store.record_schedule("daily", yesterday, {"status": "posted", "notion_url": "https://notion.so/d"})
    assert scheduler.failure_note(now) == ""


async def test_a_new_version_left_unrestarted_is_reported_once_after_an_hour(env, monkeypatch):
    scheduler, assistant, *_ = env
    troubles = []

    async def trouble(text):
        troubles.append(text)

    monkeypatch.setattr(assistant, "notify_trouble", trouble)
    monkeypatch.setattr(schedule_module.version, "RUNNING", "old")
    monkeypatch.setattr(schedule_module.version, "on_disk", lambda: "new")
    start = datetime(2026, 9, 27, 10, 0)

    for minutes in (0, 30, 60, 120, 180):
        await scheduler.notify_unrestarted(start + timedelta(minutes=minutes))

    trouble, = troubles
    assert "new" in trouble and "deploy/restart-all.sh" in trouble
