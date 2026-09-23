import asyncio
import json
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

import pytest
from fakes import FakeClaude, FakeNotion, FakePueue, FakeSlack

from kei_agent import morning, runner, themes
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager
from kei_agent.schedule import Scheduler, due_day, search_keywords


REVIEW_REPLY = """*今日の成果*
なし

*未完了タスク*
なし

夜間に実行したいタスクはありますか？"""


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "01_overview", "C9": "00_kei-agent"})
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
    assistant.notion.create_note("条件Bの考察", "考察", "2026-09-17", "質問を先に見せると精度が上がる")
    claude.behaviors = [{"text": "今日の Daily", "session_id": "daily-sess"}]

    detail = await scheduler.run_daily("2026-09-18")

    call, = claude.calls
    assert call["cwd"] == config.overview_dir
    digest = config.overview_dir / ".kei-agent" / "digest" / "2026-09-18-daily.md"
    text = digest.read_text()
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
    note = assistant.notion.notes[-1]
    assert (note.title, note.kind, note.body) == ("Daily 9/18（金）", "Daily", "今日の Daily")
    assert detail["notion_url"] == note.url


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


async def test_review_prepares_file_and_notion_and_syncs_conclusion(env, config, store):
    scheduler, assistant, slack, claude = env
    review = config.overview_dir / "reviews" / "2026-09-18.md"

    def write_review(cwd):
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("# 振り返り 2026-09-18\n\n## Codex での振り返り\n")

    claude.behaviors = [{"text": REVIEW_REPLY, "side_effect": write_review}]
    await scheduler.run_review("2026-09-18")

    assert "reviews/2026-09-18.md" in claude.calls[0]["prompt"]
    texts = slack.texts()
    assert texts[0] == "🌙 Retro & Planning 9/18（金）" and texts[1] == REVIEW_REPLY
    note = assistant.notion.notes[-1]
    assert note.kind == "振り返り" and "## Codex での振り返り" in note.body
    assert "Codex App" not in texts[2] and "/reviews/" not in texts[2]
    assert note.url in texts[2] and "このスレッド" in texts[2]

    await assistant.on_message({"channel": "C5", "user": "UME", "ts": "1001.5", "thread_ts": "1001.000",
                                "text": "条件Bの差は質問の順番で説明できる"})
    while assistant.tasks:
        import asyncio
        await asyncio.gather(*list(assistant.tasks))
    (page_id, md), = assistant.notion.appended
    assert page_id == note.id and "条件Bの差は質問の順番で説明できる" in md


async def test_review_never_posts_model_progress_narration(env):
    scheduler, _assistant, slack, claude = env
    claude.behaviors = [{"text": "まず材料を確認します。\n" + REVIEW_REPLY}]

    result = await scheduler.run_review("2026-09-23")

    assert result["status"] == "error"
    assert all("まず材料を確認します" not in text for text in slack.texts())
    assert any("指定形式" in text for text in slack.texts())


async def test_review_footer_is_notion_native_without_local_path(env):
    scheduler, _assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]

    await scheduler.run_review("2026-09-23")

    footer = slack.texts()[-1]
    assert "Codex App" not in footer and "/reviews/" not in footer
    assert "このスレッド" in footer and "Notion" in footer


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
    assert detail["status"] == "error" and detail["removed"] == {"digests": 0, "sessions": 0, "thread_logs": 0, "worktrees": 0}
    assert slack.posted()[-1]["channel"] == "C9" and "バックアップに失敗" in slack.posted()[-1]["text"]


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

async def test_tick_waits_while_the_usage_limit_is_on(env, monkeypatch):
    scheduler, assistant, *_ = env
    ran = []
    monkeypatch.setattr(scheduler, "run_task", lambda name, day, record=True: ran.append(name))
    assistant.limited_until = time.time() + 3600

    await scheduler.tick(datetime.fromisoformat("2026-09-18 08:05"))
    assert ran == []


async def test_a_task_stopped_by_the_limit_runs_again_after_it_resets(env, monkeypatch):
    """上限で止まった処理は、その日の分としては記録せず、明けてからやり直す。"""
    scheduler, assistant, *_ = env
    reset = time.time() + 3600
    ran = []

    async def hits_the_limit(name, day, record=True):
        ran.append((name, day))
        assistant.limited_until = reset
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
    assistant.limited_until = 0
    await scheduler.catch_up_deferred(reset + 120)
    assert done == [("night", "2026-09-18")]
    assert scheduler.store.due_deferred("schedule", reset + 200) == []


# 授業の締切（大学エージェント）


class FakeCourseAgent:
    """大学エージェントの代わり。list-due は JSON を返す。"""
    base_url = "http://127.0.0.1:8787"

    def __init__(self, items, classes=None):
        self.items = items
        self.classes = classes or []
        self.asked = []

    async def ask(self, skill, text="", params=None):
        from kei_agent import a2a
        self.asked.append((skill, params))
        data = {}
        if skill == "list-due":
            data = {"days": (params or {}).get("days"), "items": self.items}
        elif skill == "list-classes":
            data = {"items": self.classes}
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
    assert detail == {"synced": True, "classes": 1, "dues": 1, "events": 1}


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
