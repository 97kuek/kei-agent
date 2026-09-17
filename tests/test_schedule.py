import json
import time
from datetime import datetime

import pytest

from ezra import runner, themes
from ezra.assistant import Assistant
from ezra.jobs import JobManager
from ezra.schedule import Scheduler, due_day, search_keywords
from fakes import FakeClaude, FakePueue, FakeSlack


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "research-overview", "C9": "assistant-improve"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_claude", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    return Scheduler(config, store, assistant), assistant, slack, claude


def make_theme(config, name="vlm", keywords=("vision language model counting",)):
    ws = themes.resolve(config, name)
    themes.ensure_workspace(ws)
    if keywords is not None:
        md = ws.cwd / "CLAUDE.md"
        text = md.read_text().replace("## 検索キーワード\n", "## 検索キーワード\n\n" + "\n".join(f"- {k}" for k in keywords) + "\n", 1)
        md.write_text(text)
    return ws


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
    assert ran == [("night", "2026-09-18"), ("literature", "2026-09-18"), ("daily", "2026-09-18")]


# 🌙 の夜間 Task

async def test_moon_reaction_queues_and_unqueues(env, store):
    scheduler, assistant, *_ = env
    event = {"reaction": "crescent_moon", "user": "UME", "item_user": "UME",
             "item": {"type": "message", "channel": "C1", "ts": "50.1"}}
    await assistant.on_reaction_added(event)
    assert store.count_pending_night_tasks() == 1

    await assistant.on_reaction_removed(event)
    assert store.count_pending_night_tasks() == 0


async def test_moon_reaction_ignored_for_others_and_non_theme(env, store):
    scheduler, assistant, *_ = env
    base = {"reaction": "crescent_moon", "item": {"type": "message", "channel": "C1", "ts": "50.1"}}
    await assistant.on_reaction_added({**base, "user": "USOMEONE", "item_user": "USOMEONE"})
    await assistant.on_reaction_added({**base, "user": "UME", "item_user": "UOTHER"})
    await assistant.on_reaction_added({**base, "user": "UME", "item_user": "UME", "reaction": "eyes"})
    await assistant.on_reaction_added({"reaction": "crescent_moon", "user": "UME", "item_user": "UME",
                                       "item": {"type": "message", "channel": "C9", "ts": "60.1"}})
    assert store.count_pending_night_tasks() == 0


async def test_night_runs_tasks_in_their_threads_and_marks_done(env, config, store):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    store.add_night_task("C1", "50.1")
    store.add_night_task("C1", "50.3")
    slack.replies = [
        {"ts": "50.1", "user": "UME", "text": "条件Cも回して"},
        {"ts": "50.3", "thread_ts": "50.2", "user": "UME", "text": "図を直して"},
    ]
    claude.behaviors = [{"text": "回しました"}, {"text": "直せませんでした", "is_error": True}]

    detail = await scheduler.run_night("2026-09-18")

    assert [c["thread_ts"] for c in claude.calls] == ["50.1", "50.2"]
    assert "[🌙 夜間の Task]" in claude.calls[0]["prompt"] and "条件Cも回して" in claude.calls[0]["prompt"]
    assert ("reactions_add", {"channel": "C1", "timestamp": "50.1", "name": "white_check_mark"}) in slack.calls
    assert not any(kw.get("timestamp") == "50.3" and kw.get("name") == "white_check_mark"
                   for name, kw in slack.calls if name == "reactions_add")
    assert [t["ok"] for t in detail["tasks"]] == [True, False]
    done = store.night_tasks_finished_since(0)
    assert [r["status"] for r in done] == ["done", "failed"] and done[0]["summary"] == "回しました"


async def test_night_respects_limit(env, config, store):
    scheduler, assistant, slack, claude = env
    make_theme(config)
    for i in range(7):
        store.add_night_task("C1", f"70.{i}")
    slack.replies = [{"ts": f"70.{i}", "user": "UME", "text": f"task {i}"} for i in range(7)]
    detail = await scheduler.run_night("2026-09-18")
    assert len(claude.calls) == 5 and detail["remaining"] == 2


# 先行研究

async def test_literature_posts_only_when_new(env, config, store):
    scheduler, assistant, slack, claude = env
    make_theme(config, "vlm")
    make_theme(config, "research-log", keywords=None)
    claude.behaviors = [{"text": "papers/ に変更はありません。\n\nNO_NEW_PAPERS"}]

    detail = await scheduler.run_literature("2026-09-18")

    assert detail["themes"]["vlm"]["status"] == "no_new"
    assert detail["themes"]["research-log"]["status"] == "no_keywords"
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

async def test_daily_posts_to_overview_with_digest(env, config, store):
    scheduler, assistant, slack, claude = env
    ws = make_theme(config, "vlm")
    store.upsert_thread("C1", "10.1", "vlm", "s1")
    (ws.cwd / ".ezra" / "threads").mkdir(parents=True)
    (ws.cwd / ".ezra" / "threads" / "10.1.md").write_text("# log")
    store.add_night_task("C1", "90.1")
    store.record_schedule("literature", "2026-09-18", {"themes": {"vlm": {"status": "no_new"}}})
    claude.behaviors = [{"text": "今日の Daily", "session_id": "daily-sess"}]

    detail = await scheduler.run_daily("2026-09-18")

    call, = claude.calls
    assert call["cwd"] == config.research_root / "_overview"
    digest = config.research_root / "_overview" / ".ezra" / "digest" / "2026-09-18-daily.md"
    assert str(digest) in call["prompt"] and "daily/2026-09-18.md" in call["prompt"]
    text = digest.read_text()
    assert "10.1.md" in text and "#vlm: 新着なし" in text and "次の夜に回っている Task: 1 件" in text
    header, body = slack.posted()
    assert header == {"channel": "C5", "text": "🌅 Daily 9/18（金）"}
    assert body["markdown_text"] == "今日の Daily" and detail["status"] == "posted"


async def test_digest_lists_stalled_themes(env, config, store):
    scheduler, *_ = env
    make_theme(config, "old-theme")
    store.upsert_thread("C7", "1.1", "old-theme", "s")
    store.conn.execute("UPDATE threads SET updated_at = ?", (time.time() - 5 * 86400,))
    digest = scheduler.build_digest(time.time() - 86400, time.time(), "t")
    stalled = digest.split("## 3日以上やり取りのないテーマ")[1].split("##")[0]
    assert "#old-theme" in stalled


async def test_review_prepares_file_for_codex(env, config, store):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": "今日の要約と問い"}]
    await scheduler.run_review("2026-09-18")
    assert "reviews/2026-09-18.md" in claude.calls[0]["prompt"]
    texts = slack.texts()
    assert texts[0] == "🌙 振り返りの材料 9/18（金）" and texts[1] == "今日の要約と問い"
    assert "Codex App で" in texts[2] and "reviews/2026-09-18.md" in texts[2]


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
