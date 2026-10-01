"""Daily・振り返りのモジュール（段階3の Daily・振り返りの②。modules/daily/）。

本体の定期処理 daily と review を受け持ち、朝の一覧（core.morning）を見出しに Daily を、夜に振り返りを研究全体の
チャンネルに出して、共通ホームの日別記録に残す。振り返りのスレッドに貼られた結論も、同じ行に足す。
"""

import time
from datetime import datetime, timedelta

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack, make_theme
from test_schedule import FakeCourseAgent, due_item

from kei_agent import modules, runner
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager
from kei_agent.notion_store import Note
from kei_agent.schedule import Scheduler, task_names
from kei_agent_modules.daily import texts
from kei_agent_modules.daily.module import daily_answer, review_answer

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


def material(prompt: str) -> str:
    """プロンプトに入れた材料の部分だけ。"""
    return prompt.split(texts.MATERIAL_START, 1)[1].split(texts.MATERIAL_END, 1)[0]


def final(text: str) -> str:
    return f"<<kei-agent-final>>\n{text}\n<<kei-agent-final-end>>"


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "0-overview", "C9": "0-kei-agent"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack, claude


def daily(assistant):
    return assistant.modules["daily"]


# 受け持つ定期処理

def test_the_daily_module_takes_daily_and_review(config):
    spec = modules.builtin()["daily"]
    assert spec.core_schedules == ("daily", "review") and spec.port is None
    assert (spec.actor.files, spec.actor.shell, spec.actor.web) == ("read", False, False)
    assert {u.name for u in spec.actor.use_cases} == {"daily_write", "review_write", "review_talk"}
    assert task_names(config)[-3:] == ("daily", "review", "maintenance")
    # モジュールがオフなら、Daily と振り返りは動かさない（App Home にも出さない）
    from kei_agent import settings
    off = config.__class__(**{**config.__dict__, "modules": tuple(n for n in config.modules if n != "daily")})
    assert "daily" not in task_names(off) and "daily" not in settings.schedule_names(off)


async def test_the_scheduler_runs_the_modules_daily(env, store):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": DAILY_REPLY}]
    detail = await scheduler.run_task("daily", "2026-09-24")
    assert detail["status"] == "posted" and store.schedule_ran("daily", "2026-09-24")


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

    detail = await daily(assistant).daily("2026-09-18")

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
    # チャンネルには予定だけ、Daily の見出しはスレッドの先頭
    assert header["channel"] == "C5" and header["text"].startswith("☀️") and "Daily" not in header["text"]
    assert header["text"].startswith("☀️")
    note = assistant.hub.notes[-1]
    assert (note.title, note.kind, note.body) == ("Daily 9/18（金）", "Daily", DAILY_REPLY)
    assert [n.kind for n in assistant.notion.notes] == ["考察"]
    assert detail["notion_url"] == note.url
    assert not (config.overview_dir / "daily").exists()


async def test_daily_posts_only_four_bold_sections(env):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": "手順を確認します\n<<kei-agent-final>>\n" + DAILY_REPLY + "\n<<kei-agent-final-end>>"}]

    result = await daily(assistant).daily("2026-09-24")

    assert result["status"] == "posted"
    assert "手順を確認" not in "\n".join(slack.texts())
    assert slack.texts()[1] == f"**🌅 Daily 9/24（木）**\n\n{DAILY_REPLY}"


async def test_invalid_daily_is_not_saved_to_notion(env):
    scheduler, assistant, _slack, claude = env
    claude.behaviors = [{"text": "<<kei-agent-final>>\n*今日のタスク*\nなし\n<<kei-agent-final-end>>"}]

    result = await daily(assistant).daily("2026-09-24")

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


async def test_review_is_saved_to_the_day_row_and_asks_what_was_learned(env, config, store):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]
    await daily(assistant).review("2026-09-18")

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
    # 最後に、今日学んだこと・助言を聞く（返事は振り返りの担当が受ける）
    assert texts[2].startswith("今日、職場や学校で学んだこと") and len(texts) == 3
    assert assistant.notion.notes == []
    assert not (config.overview_dir / "reviews").exists()


async def _talk(assistant, text, ts):
    import asyncio
    await assistant.on_message({"channel": "C5", "user": "UME", "ts": ts, "thread_ts": "1001.000", "text": text})
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks))


LEARNED = ('<<kei-agent-final>>\nレビューは結論から書く、を残すね。\n📒 学び\n'
           '[{"title": "レビューは結論から書く", "field": "仕事", "kind": "助言", "source": "上司との1on1", '
           '"scene": "設計レビュー", "lesson": "先に結論を言うと議論が速い", "next": "次の資料で1行目に結論"}]\n'
           '<<kei-agent-final-end>>')


async def test_the_review_thread_turns_what_was_learned_into_notes(env):
    """振り返りのスレッドでは、AI が聞き返して言語化し、まとまったら確かめずに学びのノートに残す。"""
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]
    await daily(assistant).review("2026-09-18")
    note = assistant.hub.notes[-1]

    claude.behaviors = [{"text": "<<kei-agent-final>>\nどんな場面だった？\n<<kei-agent-final-end>>"}]
    await _talk(assistant, "上司にレビューは結論からと言われた", "1001.5")
    assert slack.texts()[-1] == "どんな場面だった？"
    call = claude.calls[-1]
    assert call["actor"] == "daily" and call["use_case"] == "review_talk"        # 研究の担当ではなく振り返りの担当
    assert "上司にレビューは結論からと言われた" in call["prompt"]

    claude.behaviors = [{"text": LEARNED}]
    await _talk(assistant, "設計レビューのとき", "1001.6")
    (page_id, kept), = assistant.hub.learnings.items()
    assert kept["day"] == "2026-09-18" and kept["item"]["kind"] == "助言"
    shown = slack.texts()[-1]
    assert "📒 学びのノートに残したよ" in shown and "レビューは結論から書く" in shown and "[{" not in shown
    (row, summary), = assistant.hub.appended                                      # 日別記録にも題とリンク
    assert row == note.id and "レビューは結論から書く" in summary

    # 「直して」で、前に残したものは捨てて差し替える
    claude.behaviors = [{"text": LEARNED.replace("1行目に結論", "冒頭で結論")}]
    await _talk(assistant, "次にどう使うかを直して", "1001.7")
    assert assistant.hub.trashed == [page_id] and len(assistant.hub.learnings) == 2


async def test_review_never_posts_model_progress_narration(env):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": "まず材料を確認します。\n" + REVIEW_REPLY}]

    result = await daily(assistant).review("2026-09-23")

    assert result["status"] == "error"
    assert all("まず材料を確認します" not in text for text in slack.texts())
    assert any("振り返りを利用者向けの形に整えられなかったよ" in text for text in slack.texts())


async def test_review_does_not_post_extra_footer(env):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]

    await daily(assistant).review("2026-09-23")

    assert slack.texts()[:2] == ["🌙 Retro & Planning 9/23（水）", REVIEW_REPLY]
    assert len(slack.texts()) == 3 and slack.texts()[2].startswith("今日、職場や学校で学んだこと")


async def test_review_without_hub_never_writes_research_notes(env, config):
    scheduler, assistant, slack, claude = env
    assistant.hub = None
    claude.behaviors = [{"text": REVIEW_REPLY}]

    result = await daily(assistant).review("2026-09-23")

    assert result["notion_url"] is None
    assert assistant.notion.notes == []
    # Slack には出し、日別記録に残せなかったことを知らせる。代わりのファイルは作らない
    assert REVIEW_REPLY in slack.texts()
    notice, = [kw for kw in slack.posted() if kw["channel"] == "C9"]
    assert "日別記録に保存できませんでした" in notice["text"]
    assert not (config.overview_dir / "reviews").exists()


async def test_daily_without_hub_still_posts_and_says_so(env, config):
    scheduler, assistant, slack, claude = env
    assistant.hub = None
    claude.behaviors = [{"text": DAILY_REPLY}]

    result = await daily(assistant).daily("2026-09-24")

    assert result["status"] == "posted" and result["notion_url"] is None
    assert f"**🌅 Daily 9/24（木）**\n\n{DAILY_REPLY}" in slack.texts()
    assert any("Daily 9/24（木） を日別記録に保存できませんでした。共通 Notion ホームが使えません" in t
               for t in slack.texts())
    assert not (config.overview_dir / "daily").exists()
    # 人の時間は読めないと材料に書く（落ちない）
    assert "共通 Notion ホームが使えない" in material(claude.calls[0]["prompt"])


async def test_review_syncs_assignments_first_and_lists_near_deadlines(env):
    """明日の計画に使うので、振り返りの前に取り込み、明日・明後日の締切をスレッドと日別記録に並べる。"""
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
    later = datetime.now() + timedelta(days=10)
    agent = FakeCourseAgent([due_item(tomorrow.isoformat(), "レポート", "情報"),
                             due_item(later.isoformat(), "期末", "情報", uid="2@moodle")])
    assistant.agents["course"] = agent

    result = await daily(assistant).review("2026-09-26")

    skills = [skill for skill, _ in agent.asked]
    assert result["prepared"] == [] and skills.index("sync-assignments") < skills.index("list-due")
    post, = [p for p in slack.posted() if p.get("text", "").startswith("📌 明日・明後日の締切")]
    assert post.get("thread_ts") and "情報 レポート" in post["text"] and "期末" not in post["text"]
    note = assistant.hub.notes[-1]
    assert "📌 明日・明後日の締切" in note.body and note.body.endswith("明日やることは何か")


def test_claude_limit_does_not_block_codex_daily(env, store):
    from kei_agent import settings

    scheduler, _, *_ = env
    settings.set_agent_provider(store, "daily", "codex")
    store.set_limit_until("claude", time.time() + 3600)
    assert scheduler.can_run("daily", time.time())


# 答えの形

def test_the_daily_answer_needs_the_four_sections():
    assert daily_answer(final(DAILY_REPLY)) == DAILY_REPLY
    assert daily_answer(final("*今日のタスク*\nなし")) == ""
    assert daily_answer("marker のない答え") == ""


VALID_REVIEW = REVIEW_REPLY


def test_the_review_answer_adds_the_night_question_when_missing():
    body = "**今日の成果**\n- 実験を回した\n\n\n**未完了タスク**\nなし"
    expected = "**今日の成果**\n- 実験を回した\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？"
    assert review_answer(final(body)) == expected
    assert review_answer(final(body + "\n\n夜間に実行したいタスクはありますか？")) == expected
    assert review_answer(final(VALID_REVIEW)) == VALID_REVIEW
    # Web のリンクと、作業場の中のファイル名は通す
    linked = VALID_REVIEW.replace("なし", "資料: https://example.com/notes", 1)
    assert review_answer(final(linked)) == linked
    named = VALID_REVIEW.replace("なし", "振り返りを reviews/2026-09-24.md にまとめた", 1)
    assert review_answer(final(named)) == named


@pytest.mark.parametrize("text", [
    "まず材料を確認します。\n" + VALID_REVIEW,
    VALID_REVIEW + "\nCodex App を開いてください",
    "**今日の成果**\n\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "# 今日\n" + VALID_REVIEW,
    "**今日の成果**\nBash で材料を読みました。\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\nSkill を使って調査中です。\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
])
def test_the_review_answer_refuses_other_shapes(text):
    assert review_answer(final(text)) == ""


@pytest.mark.parametrize("path", ["/Users/keitaro/private", "/tmp/private.md", "file:///private/private.md",
                                  "~/.config/private"])
def test_the_review_answer_shows_only_the_file_name_of_local_paths(path):
    """手元のパスは、答えごと捨てずにファイル名だけにする（Slack にも日別記録にも、場所を出さない）。"""
    shown = review_answer(final(f"**今日の成果**\n{path}\n\n**未完了タスク**\nなし"))
    assert shown and path not in shown and path.rsplit("/", 1)[-1] in shown

