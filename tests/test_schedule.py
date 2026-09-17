import json
import time
from datetime import datetime

import pytest

from ezra import runner, themes
from ezra.assistant import Assistant
from ezra.jobs import JobManager
from ezra.schedule import Scheduler, due_day, search_keywords
from fakes import FakeClaude, FakeNotion, FakePueue, FakeSlack


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "research-overview", "C9": "assistant-improve"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_claude", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/")
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

    await scheduler.tick(datetime.fromisoformat("2026-09-18 22:10"))
    assert ran[-2:] == [("review", "2026-09-18"), ("maintenance", "2026-09-18")]


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
    assert slack.texts()[-1].startswith("🌙 今夜の Task にしました")

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
    assert posted[0]["text"] == "⚠️ Notion に Task を作れませんでした"
    assert posted[1]["channel"] == "C9" and "503" in posted[1]["text"]


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
    assert slack.posted()[-1]["channel"] == "C9" and "今夜は実行しません" in slack.posted()[-1]["text"]


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

async def test_daily_posts_to_overview_and_notion(env, config, store):
    scheduler, assistant, slack, claude = env
    ws = make_theme(config, "vlm")
    store.upsert_thread("C1", "10.1", "vlm", "s1")
    (ws.cwd / ".ezra" / "threads").mkdir(parents=True)
    (ws.cwd / ".ezra" / "threads" / "10.1.md").write_text("# log")
    store.record_schedule("literature", "2026-09-18", {"themes": {"vlm": {"status": "no_new"}}})
    store.record_schedule("night", "2026-09-18", {"status": "done", "tasks": [
        {"title": "条件Cも回して", "theme": "vlm", "status": "完了", "summary": "71%", "url": "https://notion.example/t"}]})
    assistant.notion.add_task("返事が要る", "vlm", status="確認待ち")
    assistant.notion.create_note("条件Bの考察", "考察", "2026-09-17", "質問を先に見せると精度が上がる")
    claude.behaviors = [{"text": "今日の Daily", "session_id": "daily-sess"}]

    detail = await scheduler.run_daily("2026-09-18")

    call, = claude.calls
    assert call["cwd"] == config.research_root / "_overview"
    digest = config.research_root / "_overview" / ".ezra" / "digest" / "2026-09-18-daily.md"
    text = digest.read_text()
    assert "10.1.md" in text and "#vlm: 新着なし" in text
    assert "条件Cも回して（#vlm）: 完了 71%" in text
    assert "### 考察: 条件Bの考察" in text and "質問を先に見せると精度が上がる" in text
    assert "返事が要る" in text and "中間発表" in text
    header, body = slack.posted()
    assert header == {"channel": "C5", "text": "🌅 Daily 9/18（金）"}
    note = assistant.notion.notes[-1]
    assert (note.title, note.kind, note.body) == ("Daily 9/18（金）", "Daily", "今日の Daily")
    assert detail["notion_url"] == note.url


async def test_digest_lists_stalled_themes(env, config, store):
    scheduler, *_ = env
    make_theme(config, "old-theme")
    store.upsert_thread("C7", "1.1", "old-theme", "s")
    store.conn.execute("UPDATE threads SET updated_at = ?", (time.time() - 5 * 86400,))
    digest = await scheduler.build_digest(time.time() - 86400, time.time(), "t")
    stalled = digest.split("## 3日以上やり取りのないテーマ")[1].split("##")[0]
    assert "#old-theme" in stalled


async def test_review_prepares_file_and_notion_and_syncs_conclusion(env, config, store):
    scheduler, assistant, slack, claude = env
    review = config.research_root / "_overview" / "reviews" / "2026-09-18.md"

    def write_review(cwd):
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("# 振り返り 2026-09-18\n\n## Codex での振り返り\n")

    claude.behaviors = [{"text": "今日の要約と問い", "side_effect": write_review}]
    await scheduler.run_review("2026-09-18")

    assert "reviews/2026-09-18.md" in claude.calls[0]["prompt"]
    texts = slack.texts()
    assert texts[0] == "🌙 振り返りの材料 9/18（金）" and texts[1] == "今日の要約と問い"
    note = assistant.notion.notes[-1]
    assert note.kind == "振り返り" and "## Codex での振り返り" in note.body
    assert "Codex App で" in texts[2] and note.url in texts[2]

    await assistant.on_message({"channel": "C5", "user": "UME", "ts": "1001.5", "thread_ts": "1001.000",
                                "text": "条件Bの差は質問の順番で説明できる"})
    while assistant.tasks:
        import asyncio
        await asyncio.gather(*list(assistant.tasks))
    (page_id, md), = assistant.notion.appended
    assert page_id == note.id and "条件Bの差は質問の順番で説明できる" in md


async def test_member_joined_registers_theme_in_notion(env, config):
    scheduler, assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
    assert assistant.notion.themes == {"vlm": "https://example.slack.com/archives/C1"}


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


async def test_maintenance_reports_backup_failure(env, config):
    scheduler, assistant, slack, claude = env
    config.research_root.mkdir(parents=True, exist_ok=True)  # Git のリポジトリではない
    detail = await scheduler.run_maintenance("2026-09-18")
    assert detail["status"] == "error" and detail["removed"] == {"digests": 0, "sessions": 0}
    assert slack.posted()[-1]["channel"] == "C9" and "バックアップに失敗" in slack.posted()[-1]["text"]
