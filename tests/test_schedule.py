import asyncio
import json
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

import pytest
from fakes import FakeClaude, FakeNotion, FakePueue, FakeSlack

from kei_agent import agents, morning, runner, themes
from kei_agent import schedule as schedule_module
from kei_agent.assistant import Assistant
from kei_agent.calendar_sync import SyncReport
from kei_agent.jobs import JobManager
from kei_agent.notion_store import Note
from kei_agent.schedule import Scheduler, due_day, search_keywords

REVIEW_REPLY = """**今日の成果**
なし

**未完了タスク**
なし

夜間に実行したいタスクはありますか？"""

DAILY_REPLY = """**今日のタスク**
なし

**夜間処理の結果**
なし

**確認待ち・期日・止まっているテーマ・返事待ち**
なし

**今日考えるとよい問い**
1. 僕は何から進めよう？"""


class FakeHub:
    has_time_db = True

    def __init__(self):
        self.notes = []
        self.appended = []
        self.reviews: dict[str, str] = {}
        self.edited: list[Note] = []
        self.minutes: dict[str, float] = {}
        self.known: set[str] = set()
        self.recorded = []
        self.calendar: list[dict] = []

    def upsert_day(self, kind, day, title, markdown, slack_url):
        note = Note(f"hub-{day}", title, kind, day, f"https://notion.example/hub/{day}", markdown)
        self.notes.append(note)
        return note

    def append_review_conclusion(self, page_id, text, stamp, message_id=None):
        self.appended.append((page_id, text))

    def reviews_edited_since(self, since):
        return list(self.edited)

    def review_text(self, day):
        return self.reviews.get(day, "")

    def time_minutes_by_domain(self, start, days=7):
        return dict(self.minutes)

    def time_url(self):
        return "https://www.notion.so/timedb"

    def time_ids_since(self, since):
        return set(self.known)

    def record_time(self, entry_id, domain, label, started_at, minutes, memo="", slack_url="", source="Slack"):
        self.recorded.append((entry_id, domain, label, minutes, source))

    def schema_problems(self):
        return []

    def calendar_rows(self, source, window_start, window_end):
        return [dict(row) for row in self.calendar if row["出典"] == source]

    def calendar_upsert(self, source, item, checked_at, existing_id=None):
        if existing_id:
            next(row for row in self.calendar if row["id"] == existing_id).update(
                {"名前": item.title, "日付": item.start, "同期状態": "確認済み"})
            return
        self.calendar.append({"id": f"cal-{len(self.calendar)}", "出典": source, "出典 ID": item.source_id,
                              "名前": item.title, "日付": item.start, "同期状態": "確認済み"})

    def calendar_mark_stale(self, row_id):
        next(row for row in self.calendar if row["id"] == row_id)["同期状態"] = "要確認"


def material(prompt: str) -> str:
    """プロンプトに入れた材料の部分だけ。"""
    return prompt.split(schedule_module.MATERIAL_START, 1)[1].split(schedule_module.MATERIAL_END, 1)[0]


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "01_overview", "C9": "00_kei-agent"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack, claude


def make_theme(config, name="vlm", keywords=("vision language model counting",)):
    ws = themes.resolve(config, name)
    themes.ensure_workspace(ws)
    if keywords is not None:
        md = ws.cwd / "CLAUDE.md"
        text = md.read_text().replace("## 検索キーワード\n", "## 検索キーワード\n\n" + "\n".join(f"- {k}" for k in keywords) + "\n", 1)
        md.write_text(text)
    return ws


async def test_hub_calendar_copies_all_future_assignments_without_asking_work(env, monkeypatch):
    """課題はこれからの全部を写す。会議は朝の Daily で書くので、ここでは仕事の担当（AI）に聞かない。"""
    scheduler, assistant, *_ = env
    asked, seen = [], []

    async def ask_course(skill, **params):
        asked.append((skill, params))
        return agents.Reply(data={"complete": True, "items": [
            {"id": "assignment-1", "title": "課題", "due": "2027-02-01",
             "status": "未着手", "url": "https://notion.so/assignment-1"}]})

    async def ask_work(skill, **params):
        raise AssertionError("予定カレンダーのために仕事の担当を動かさない")

    def record_sync(hub, snapshot, checked_at, days):
        seen.append((snapshot.source, [i.source_id for i in snapshot.items], days))
        return SyncReport(1, 0, 0)

    monkeypatch.setattr(assistant, "ask_course", ask_course)
    monkeypatch.setattr(assistant, "ask_work", ask_work)
    monkeypatch.setattr(schedule_module, "sync_calendar", record_sync)

    result = await scheduler.sync_hub_calendar("2026-09-26")

    assert result == {"course": "synced", "course_counts": {"created": 1, "updated": 0, "stale": 0}}
    assert asked == [("list-calendar-assignments", {"days": schedule_module.COURSE_CALENDAR_DAYS})]
    assert seen == [("課題", ["assignment-1"], schedule_module.COURSE_CALENDAR_DAYS)]


async def test_hub_calendar_sync_without_hub_does_not_call_agents(env, monkeypatch):
    scheduler, assistant, *_ = env
    assistant.hub = None

    async def unexpected(*args, **kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(assistant, "ask_course", unexpected)
    monkeypatch.setattr(assistant, "ask_work", unexpected)
    assert await scheduler.sync_hub_calendar("2026-09-24") == {"course": "no_hub"}


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


async def test_hub_calendar_runs_without_daily_and_retries_after_failed_hour(env, monkeypatch):
    scheduler, assistant, *_ = env
    runs = []

    async def fake_run(name, day, record=True):
        runs.append((name, day))
        return {"course": "error"}

    async def noop(*args):
        return None

    monkeypatch.setattr(schedule_module.settings, "schedule_time", lambda *args: "")
    monkeypatch.setattr(scheduler, "run_task", fake_run)
    monkeypatch.setattr(scheduler, "notify_due_soon", noop)
    monkeypatch.setattr(scheduler, "nudge_stale_threads", noop)
    await scheduler.tick(datetime.fromisoformat("2026-09-18T08:05"))
    await scheduler.tick(datetime.fromisoformat("2026-09-18T08:06"))
    await scheduler.tick(datetime.fromisoformat("2026-09-18T09:06"))
    assert runs == [("hub_calendar", "2026-09-18"), ("hub_calendar", "2026-09-18")]


async def test_hub_calendar_is_done_for_the_day_once_assignments_are_copied(env, monkeypatch):
    scheduler, assistant, *_ = env
    runs = []

    async def fake_run(name, day, record=True):
        runs.append((name, day))
        return {"course": "synced"}

    async def noop(*args):
        return None

    monkeypatch.setattr(schedule_module.settings, "schedule_time", lambda *args: "")
    monkeypatch.setattr(scheduler, "run_task", fake_run)
    monkeypatch.setattr(scheduler, "notify_due_soon", noop)
    monkeypatch.setattr(scheduler, "nudge_stale_threads", noop)
    await scheduler.tick(datetime.fromisoformat("2026-09-18T08:05"))
    await scheduler.tick(datetime.fromisoformat("2026-09-18T10:06"))
    assert runs == [("hub_calendar", "2026-09-18")]


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
    assert search_keywords(ws.cwd / "CLAUDE.md") == []
    ws = make_theme(config, name="other", keywords=["vlm counting", "object counting benchmark"])
    assert search_keywords(ws.cwd / "CLAUDE.md") == ["vlm counting", "object counting benchmark"]


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
    # 01:30 の夜間、07:00 の先行研究、08:00 の Daily が1回ずつ。21:00 はまだ
    assert ran == [("night", "2026-09-18"), ("literature", "2026-09-18"),
                   ("daily", "2026-09-18"), ("hub_calendar", "2026-09-18")]

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


# 先行研究

async def test_literature_posts_only_when_new(env, config, store):
    scheduler, assistant, slack, claude = env
    make_theme(config, "vlm")
    slack.channels["C2"] = "notes"
    make_theme(config, "notes", keywords=None)
    make_theme(config, "archived")  # Kei Agent のいない（アーカイブした）テーマは見張らない
    claude.behaviors = [{"text": "papers/ に変更はありません。\n\nNO_NEW_PAPERS"}]

    detail = await scheduler.run_literature("2026-09-18")

    assert detail["themes"] == {"vlm": {"status": "no_new"}, "notes": {"status": "no_keywords"}}
    assert slack.posted() == []
    assert "vision language model counting" in claude.calls[0]["prompt"]

    claude.behaviors = [{"text": "新着が2本あります", "session_id": "lit-sess"}]
    detail = await scheduler.run_literature("2026-09-19")
    header, body = slack.posted()
    assert header == {"channel": "C1", "text": "📚 先行研究の新着 9/19（土）"}
    assert body["thread_ts"] == "1001.000" and body["markdown_text"] == "新着が2本あります"
    # スレッドで続きを話せる
    assert store.get_thread("C1", "1001.000")["session_id"] == "lit-sess"


# Daily と振り返り

async def test_daily_posts_to_overview_and_notion(env, config, store):
    scheduler, assistant, slack, claude = env
    ws = make_theme(config, "vlm")
    store.upsert_thread("C1", "10.1", "vlm", "s1")
    (ws.cwd / ".kei-agent" / "threads").mkdir(parents=True)
    (ws.cwd / ".kei-agent" / "threads" / "10.1.md").write_text("# log")
    store.record_schedule("literature", "2026-09-18", {"themes": {"vlm": {"status": "no_new"}}})
    store.record_schedule("night", "2026-09-18", {"status": "done", "tasks": [
        {"title": "条件Cも回して", "theme": "vlm", "status": "完了", "summary": "71%", "url": "https://notion.example/t"}]})
    assistant.notion.add_task("返事が要る", "vlm", status="確認待ち")
    assistant.notion.notes.append(Note("note-1", "条件Bの考察", "考察", "2026-09-17",
                                       "https://notion.example/note-1", "質問を先に見せると精度が上がる"))
    claude.behaviors = [{"text": DAILY_REPLY, "session_id": "daily-sess"}]

    detail = await scheduler.run_daily("2026-09-18")

    call, = claude.calls
    assert call["cwd"] == config.overview_dir
    # 材料はファイルにせず、プロンプトにそのまま入れる。Daily のファイルもグラフも作らせない
    assert not (config.overview_dir / ".kei-agent" / "digest").exists()
    assert "daily/" not in call["prompt"] and "outputs/" not in call["prompt"]
    text = material(call["prompt"])
    assert "10.1.md" in text
    # 先行研究はテーマのチャンネルに流すので、Daily の材料には入れない（2026-09-22）
    assert "先行研究" not in text
    # 「今日のタスク」の元になる（済みも入れて、取り消し線にする）
    assert "## Notion: 今日が期日の Task（済みを含む）" in text
    assert "条件Cも回して（vlm）: 完了 71%" in text
    assert "### 考察: 条件Bの考察" in text and "質問を先に見せると精度が上がる" in text
    assert "返事が要る" in text and "中間発表" in text
    header, body = slack.posted()
    # 見出しには、朝の時系列（今日の予定）と Daily の題を1通にまとめて出す
    assert header["channel"] == "C5" and header["text"].endswith("🌅 Daily 9/18（金）")
    assert header["text"].startswith("☀️")
    note = assistant.hub.notes[-1]
    assert (note.title, note.kind, note.body) == ("Daily 9/18（金）", "Daily", DAILY_REPLY)
    assert [n.kind for n in assistant.notion.notes] == ["考察"]
    assert detail["notion_url"] == note.url
    assert not (config.overview_dir / "daily").exists()


async def test_daily_posts_only_four_bold_sections(env):
    scheduler, _assistant, slack, claude = env
    claude.behaviors = [{"text": "手順を確認します\n<<kei-agent-final>>\n" + DAILY_REPLY + "\n<<kei-agent-final-end>>"}]

    result = await scheduler.run_daily("2026-09-24")

    assert result["status"] == "posted"
    assert "手順を確認" not in "\n".join(slack.texts())
    assert slack.texts()[1] == DAILY_REPLY


async def test_invalid_daily_is_not_saved_to_notion(env):
    scheduler, assistant, _slack, claude = env
    claude.behaviors = [{"text": "<<kei-agent-final>>\n*今日のタスク*\nなし\n<<kei-agent-final-end>>"}]

    result = await scheduler.run_daily("2026-09-24")

    assert result["status"] == "error"
    assert assistant.notion.notes == []
    assert assistant.hub.notes == []


async def test_digest_lists_stalled_and_waiting_only_for_active_channels(env, config, store):
    from kei_agent.digest import DigestBuilder
    scheduler, assistant, *_ = env
    make_theme(config, "old-theme")
    make_theme(config, "archived")
    store.upsert_thread("C7", "1.1", "old-theme", "s")
    store.upsert_thread("C8", "2.1", "archived", "s")
    store.conn.execute("UPDATE threads SET updated_at = ?", (time.time() - 5 * 86400,))
    store.set_awaiting("C7", "1.1", True)
    store.set_awaiting("C8", "2.1", True)

    digest = await DigestBuilder(config, store, assistant).build(
        time.time() - 86400, time.time(), "t", {"old-theme"})

    stalled = digest.split("## 3日以上やり取りのないテーマ")[1].split("##")[0]
    waiting = digest.split("## 返事待ちのスレッド")[1].split("##")[0]
    assert "#old-theme" in stalled and "archived" not in stalled
    assert "#old-theme" in waiting and "archived" not in waiting


async def test_digest_skips_theme_never_asked(env, config, store):
    """招待しただけで一度も依頼のないテーマは、止まっているテーマに数えない。"""
    import os

    from kei_agent.digest import DigestBuilder
    scheduler, assistant, *_ = env
    ws = make_theme(config, "just-invited")
    old = time.time() - 5 * 86400
    os.utime(ws.cwd, (old, old))

    digest = await DigestBuilder(config, store, assistant).build(
        time.time() - 86400, time.time(), "t", {"just-invited"})

    stalled = digest.split("## 3日以上やり取りのないテーマ")[1].split("##")[0]
    assert "just-invited" not in stalled and "なし" in stalled


async def test_review_is_saved_only_to_the_day_row_and_syncs_conclusion(env, config, store):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]
    await scheduler.run_review("2026-09-18")

    prompt = claude.calls[0]["prompt"]
    # 材料はプロンプトに入れ、振り返りのファイルは書かせない（Notion の日別記録だけに残す）
    assert "reviews/" not in prompt and "今日が期日の Task" in material(prompt)
    texts = slack.texts()
    assert texts[0] == "🌙 Retro & Planning 9/18（金）" and texts[1] == REVIEW_REPLY
    note = assistant.hub.notes[-1]
    # 日別記録には、振り返るときの問いも残す（Slack には出さない）
    assert note.kind == "振り返り" and note.body.startswith(REVIEW_REPLY)
    assert "振り返りの問い" in note.body and "明日やることは何か" in note.body
    assert "振り返りの問い" not in "".join(texts)
    assert len(texts) == 2
    assert assistant.notion.notes == []

    await assistant.on_message({"channel": "C5", "user": "UME", "ts": "1001.5", "thread_ts": "1001.000",
                                "text": "条件Bの差は質問の順番で説明できる"})
    while assistant.tasks:
        import asyncio
        await asyncio.gather(*list(assistant.tasks))
    (page_id, conclusion), = assistant.hub.appended
    assert page_id == note.id and "条件Bの差は質問の順番で説明できる" in conclusion
    assert not (config.overview_dir / "reviews").exists()


async def test_review_never_posts_model_progress_narration(env):
    scheduler, _assistant, slack, claude = env
    claude.behaviors = [{"text": "まず材料を確認します。\n" + REVIEW_REPLY}]

    result = await scheduler.run_review("2026-09-23")

    assert result["status"] == "error"
    assert all("まず材料を確認します" not in text for text in slack.texts())
    assert any("振り返りを利用者向けの形に整えられなかったよ" in text for text in slack.texts())


async def test_review_does_not_post_extra_footer(env):
    scheduler, _assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]

    await scheduler.run_review("2026-09-23")

    assert slack.texts() == ["🌙 Retro & Planning 9/23（水）", REVIEW_REPLY]


async def test_review_without_hub_never_writes_research_notes(env, config):
    scheduler, assistant, slack, claude = env
    assistant.hub = None
    claude.behaviors = [{"text": REVIEW_REPLY}]

    result = await scheduler.run_review("2026-09-23")

    assert result["notion_url"] is None
    assert assistant.notion.notes == []
    # Slack には出し、日別記録に残せなかったことを知らせる。代わりのファイルは作らない
    assert REVIEW_REPLY in slack.texts()
    notice = slack.posted()[-1]
    assert notice["channel"] == "C9" and "日別記録に保存できませんでした" in notice["text"]
    assert not (config.overview_dir / "reviews").exists()


async def test_daily_without_hub_still_posts_and_says_so(env, config):
    scheduler, assistant, slack, claude = env
    assistant.hub = None
    claude.behaviors = [{"text": DAILY_REPLY}]

    result = await scheduler.run_daily("2026-09-24")

    assert result["status"] == "posted" and result["notion_url"] is None
    assert DAILY_REPLY in slack.texts()
    assert any("Daily 9/24（木） を日別記録に保存できませんでした。共通 Notion ホームが使えません" in t
               for t in slack.texts())
    assert not (config.overview_dir / "daily").exists()
    # 人の時間は読めないと材料に書く（落ちない）
    assert "共通 Notion ホームが使えない" in material(claude.calls[0]["prompt"])


async def test_member_joined_registers_theme_in_notion(env, config):
    scheduler, assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
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
    assert detail["status"] == "error" and detail["removed"] == {"sessions": 0, "thread_logs": 0, "worktrees": 0}
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


def test_claude_limit_does_not_block_codex_daily(env, store):
    from kei_agent import settings

    scheduler, _, *_ = env
    settings.set_agent_provider(store, "router", "codex")
    store.set_limit_until("claude", time.time() + 3600)
    assert scheduler.can_run("daily", time.time())


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
    assert done == [("night", "2026-09-18"), ("literature", "2026-09-18"),
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

    async def ask(self, skill, text="", params=None):
        from kei_agent import a2a
        self.asked.append((skill, params))
        data = {}
        if skill == "list-due":
            data = {"days": (params or {}).get("days"), "items": self.items}
        elif skill == "list-classes":
            data = {"items": self.classes}
        elif skill == "sync-assignments":
            data = dict(self.synced)
        elif skill == "list-calendar-assignments":
            data = {"complete": True, "items": self.assignments}
        # 返事は全エージェント共通の封筒
        envelope = {"ok": True, "text": f"{skill} をやったよ", "data": data,
                    "limit_reset_at": None, "cost_usd": None}
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps(envelope, ensure_ascii=False))


class FakeWorkAgent:
    """仕事エージェントの代わり（Outlook の予定）。"""
    base_url = "http://127.0.0.1:8789"

    def __init__(self, items):
        self.items = items
        self.asked = []

    async def ask(self, skill, text="", params=None):
        from kei_agent import a2a
        self.asked.append((skill, params))
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
    # 読んだ会議は予定カレンダーにも書く（AI をもう一度動かさない）
    assert detail == {"synced": True, "classes": 1, "dues": 1, "events": 1,
                      "meetings": {"created": 1, "updated": 0, "stale": 0}}
    assert [(row["出典"], row["名前"]) for row in assistant.hub.calendar] == [("Outlook", "朝会")]
    assert [skill for skill, _ in assistant.agents["work"].asked] == ["list-events"]


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
    """朝のまとめに出した締切は、そのあとの24時間前の知らせで繰り返さない。"""
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    soon = (datetime.now() + timedelta(hours=5)).astimezone().isoformat()
    assistant.agents["course"] = FakeCourseAgent([due_item(soon)])

    _, _, notices = await scheduler.morning_text(datetime.now())
    for key in notices:
        scheduler.store.record_notice(key)
    before = len(slack.posted())
    await scheduler.notify_due_soon(datetime.now())

    assert len(slack.posted()) == before


async def test_due_check_retries_immediately_after_the_agent_fails(env, monkeypatch):
    """一時的に一覧を取れなくても、1時間待たず次の tick で取り直す。"""
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    calls = 0

    async def course_due(days, now):
        nonlocal calls
        calls += 1
        return None if calls == 1 else []

    monkeypatch.setattr(assistant, "course_due", course_due)
    now = datetime.now()

    await scheduler.notify_due_soon(now)
    await scheduler.notify_due_soon(now)

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
        classes=[{"subject": "マルチメディア工学A", "start": "", "end": ""}])
    assistant.agents["work"] = FakeWorkAgent(
        [{"subject": "定例MTG", "start": f"{today}T18:00", "end": f"{today}T19:00"},
         {"subject": "ゆうちょ様AML", "start": f"{tomorrow}T11:00", "end": f"{tomorrow}T13:00"}])

    text = await DigestBuilder(config, store, assistant).build(
        now.timestamp() - 86400, now.timestamp(), "振り返りの材料", set(), domains=True)

    assert "## 大学" in text and "## 仕事" in text
    assert "今日が期限だったもの: プロジェクト研究B / 履修申請フォーム（17:00）" in text
    assert "第3回レポート" in text.split("残っている締切:")[1].splitlines()[0]
    assert "明日（" in text and "マルチメディア工学A" in text
    assert "今日あった会議: 18:00–19:00 定例MTG" in text
    assert "明日の会議: 11:00–13:00 ゆうちょ様AML" in text


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
    """前日の振り返りは日別記録から、今週の時間は時間記録と runs から読む（手元のファイルは見ない）。"""
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
    week = text.split("## 時間（今週）")[1].split("\n## ")[0]
    assert "合計 2.0 時間（研究 1.5 時間、大学 0.5 時間）" in week
    assert "Kei Agent の稼働: 0.5 時間" in week
    assert "https://www.notion.so/timedb" in week


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


async def test_maintenance_imports_toggl_only_entries(env, store, monkeypatch):
    """Toggl のアプリで直接測った分は、毎晩の保守で時間記録に入れる。Slack から送った分は重ねない。"""
    from kei_agent import maintenance, timelog

    scheduler, assistant, *_ = env
    started = time.time() - 3600
    entry = store.start_time_entry("e1", "UME", "research", "C1", "vlm", "", "", "研究 / vlm", started, "done")[0]
    store.finish_time_entry("UME", started + 1500)

    class Toggl:
        def entries(self, since, until):
            iso = datetime.fromtimestamp(entry["started_at"]).astimezone().isoformat()
            return [{"id": 1, "start": iso, "duration": 1500, "project": {"name": "研究 / vlm"}},
                    {"id": 2, "start": "2026-09-17T10:00:00+09:00", "duration": 600,
                     "project": {"name": "仕事/定例"}}]

    monkeypatch.setattr(timelog, "load_toggl", lambda: Toggl())
    monkeypatch.setattr(maintenance, "cleanup", lambda *a: {})
    object.__setattr__(scheduler.config.maintenance, "backup", False)

    detail = await scheduler.run_maintenance("2026-09-18")

    assert assistant.hub.recorded == [("toggl:2", "仕事", "定例", 10, "Toggl")]
    assert detail["toggl"]["imported"] == 1 and detail["toggl"]["own"] == 1


async def test_toggl_import_failure_does_not_stop_maintenance(env, monkeypatch):
    from kei_agent import maintenance, timelog

    scheduler, assistant, *_ = env

    class Broken:
        def entries(self, since, until):
            raise timelog.TogglError("GET /time-entries: 503")

    monkeypatch.setattr(timelog, "load_toggl", lambda: Broken())
    monkeypatch.setattr(maintenance, "cleanup", lambda *a: {})
    object.__setattr__(scheduler.config.maintenance, "backup", False)

    detail = await scheduler.run_maintenance("2026-09-18")

    assert detail["status"] == "done"
    assert detail["toggl"]["status"] == "error" and "503" in detail["toggl"]["error"]


async def test_toggl_import_waits_for_the_time_db(env, monkeypatch):
    from kei_agent import timelog

    scheduler, assistant, *_ = env
    monkeypatch.setattr(timelog, "load_toggl", lambda: pytest.fail("時間記録が無いのに Toggl を読んだ"))
    assistant.hub.has_time_db = False
    assert await scheduler.import_toggl() == {"status": "skipped", "reason": "no_hub"}
    assistant.hub = None
    assert await scheduler.import_toggl() == {"status": "skipped", "reason": "no_hub"}


# Moodle の取り込みの知らせと、レトプラの締切


async def test_scheduled_sync_announces_new_and_changed_assignments(env):
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    assistant.agents["course"] = FakeCourseAgent([], synced={
        "added": ["10/26 00:00 情報 / Assignment A"], "updated": ["11/02 00:00 情報 / Assignment B"]})

    assert await scheduler.sync_assignments() is True

    post = slack.posted()[-1]
    assert post["channel"] == "C7"
    assert post["text"] == ("📚 Moodle の課題\n• 新しい: 10/26 00:00 情報 / Assignment A\n"
                            "• 締切が変わった: 11/02 00:00 情報 / Assignment B")


async def test_scheduled_sync_stays_quiet_without_changes(env):
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    assistant.agents["course"] = FakeCourseAgent([])
    before = len(slack.posted())

    assert await scheduler.sync_assignments() is True
    assert len(slack.posted()) == before


async def test_review_syncs_assignments_first_and_lists_near_deadlines(env):
    """明日の計画に使うので、振り返りの前に取り込み、明日・明後日の締切をスレッドと日別記録に並べる。"""
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
    later = datetime.now() + timedelta(days=10)
    agent = FakeCourseAgent([due_item(tomorrow.isoformat(), "レポート", "情報"),
                             due_item(later.isoformat(), "期末", "情報", uid="2@moodle")])
    assistant.agents["course"] = agent

    result = await scheduler.run_review("2026-09-26")

    skills = [skill for skill, _ in agent.asked]
    assert result["synced"] is True and skills.index("sync-assignments") < skills.index("list-due")
    post, = [p for p in slack.posted() if p.get("text", "").startswith("📌 明日・明後日の締切")]
    assert post.get("thread_ts") and "情報 レポート" in post["text"] and "期末" not in post["text"]
    note = assistant.hub.notes[-1]
    assert "📌 明日・明後日の締切" in note.body and note.body.endswith("明日やることは何か")


# 締切3日前の未着手、うまくいかなかったこと、起動し直していない新しい版


async def test_unstarted_assignments_are_noticed_three_days_ahead_once(env):
    scheduler, assistant, slack, _ = env
    slack.channels["C7"] = "20_course"
    now = datetime(2026, 10, 23, 12, 0)
    assistant.agents["course"] = FakeCourseAgent([], assignments=[
        {"id": "a", "title": "Assignment A", "due": "2026-10-26T00:00:00.000+09:00", "status": "未着手",
         "url": "https://notion.so/a"},
        {"id": "b", "title": "もう出した", "due": "2026-10-25T12:00:00.000+09:00", "status": "提出済み"},
        {"id": "c", "title": "まだ先", "due": "2026-10-30T12:00:00.000+09:00", "status": "未着手"},
    ])

    await scheduler.notify_due_soon(now)
    scheduler._due_checked = 0.0
    await scheduler.notify_due_soon(now)

    early = [p["text"] for p in slack.posted() if p.get("text", "").startswith("📚")]
    assert early == ["📚 あと 2 日で締切、まだ未着手: Assignment A\n10/25（日） 24:00 まで\nhttps://notion.so/a"]


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
