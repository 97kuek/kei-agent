import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack, write_request

from kei_agent import runner
from kei_agent.assistant import Assistant
from kei_agent.auto_messages import history_prompt
from kei_agent.jobs import JobManager
from kei_agent.request import Request
from kei_agent.slack_text import split_text


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C9": "00_kei-agent", "C5": "research-overview"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_claude", claude)
    pueue = FakePueue()
    assistant = Assistant(config, store, slack, JobManager(config, store, pueue), "xoxb-test", "UBOT")
    return assistant, slack, claude, pueue


async def settle(assistant):
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks))


async def test_mention_runs_claude_in_theme_and_replies(env, config, store):
    assistant, slack, claude, _ = env

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    call, = claude.calls
    assert call["cwd"] == config.research_root / "vlm"
    assert call["prompt"] == "図を作って" and call["session_id"] is None and call["thread_ts"] == "10.1"
    assert (config.research_root / "vlm" / "CLAUDE.md").exists()
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "eyes"}) in slack.calls
    # 作業中は agent session を processing にし、終わったら active に戻す。返事は流しながら見せる
    assert slack.statuses() == ["processing", "active"]
    # 終わったら 👀 を外して ✅ にする。どの依頼に答えたかが一目で分かる
    assert ("reactions_remove", {"channel": "C1", "timestamp": "10.1", "name": "eyes"}) in slack.calls
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "white_check_mark"}) in slack.calls
    assert slack.streamed() == ["結果です"]
    assert any(name == "chat_stopStream" for name, _ in slack.calls)
    # 経過は入力欄の下の1行だけに出し、返事には手順を並べない（長い作業でもスクロールが要らない）
    assert slack.tasks() == []
    assert slack.thinking() == ["考え中…", "Bash: テスト", "結果です"]
    assert "結果です" not in slack.texts()  # 流して見せたので、もう一度投稿しない
    assert store.get_thread("C1", "10.1")["session_id"] == "sess-1"
    log = (config.research_root / "vlm" / ".kei-agent" / "threads" / "10.1.md").read_text()
    assert "## 依頼者" in log and "図を作って" in log and "## Kei Agent" in log and "結果です" in log


async def test_mention_from_other_user_is_ignored(env):
    assistant, slack, claude, _ = env
    await assistant.on_mention({"channel": "C1", "user": "USOMEONE", "ts": "10.1", "text": "<@UBOT> hi"})
    await settle(assistant)
    assert claude.calls == [] and slack.calls == []


async def test_thread_reply_without_mention_resumes_session(env, store):
    assistant, slack, claude, _ = env
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 始めて"})
    await settle(assistant)

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.5", "thread_ts": "10.1", "text": "続きを"})
    await settle(assistant)

    assert claude.calls[1]["session_id"] == "sess-1" and claude.calls[1]["prompt"] == "続きを"


async def test_messages_that_should_not_trigger(env):
    assistant, slack, claude, _ = env
    # スレッドの外（メンションなし）
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "11.1", "text": "メモ"})
    # Kei Agent が動いていないスレッド
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "12.2", "thread_ts": "12.1", "text": "x"})
    # メンションつき（app_mention 側で処理する）
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "12.3", "thread_ts": "12.1", "text": "<@UBOT> x"})
    # Bot 自身の投稿
    await assistant.on_message({"channel": "C1", "bot_id": "B1", "ts": "12.4", "thread_ts": "12.1", "text": "x"})
    await settle(assistant)
    assert claude.calls == []


async def test_missing_session_is_restored_from_thread_history(env, store):
    assistant, slack, claude, _ = env
    store.upsert_thread("C1", "10.1", "vlm", "lost-session")
    slack.replies = [
        {"ts": "10.1", "user": "UME", "text": "<@UBOT> 条件Aで回して"},
        {"ts": "10.2", "user": "UBOT", "bot_id": "B1", "text": "⏳ 作業中"},
        {"ts": "10.3", "user": "UBOT", "bot_id": "B1", "text": "条件Aの結果は 82% でした"},
        {"ts": "10.5", "user": "UME", "text": "Bも"},
    ]
    claude.behaviors = [
        {"is_error": True, "text": "", "errors": ["No conversation found with session ID: lost-session"], "session_id": None},
        {"session_id": "sess-new"},
    ]

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.5", "thread_ts": "10.1", "text": "Bも"})
    await settle(assistant)

    first, second = claude.calls
    assert first["session_id"] == "lost-session" and second["session_id"] is None
    assert "依頼者: 条件Aで回して" in second["prompt"] and "Kei Agent: 条件Aの結果は 82% でした" in second["prompt"]
    assert "作業中" not in second["prompt"] and second["prompt"].count("Bも") == 1
    assert store.get_thread("C1", "10.1")["session_id"] == "sess-new"
    assert not any(t.startswith("⚠️") for t in slack.texts())


async def test_long_thread_history_is_paged_and_says_what_it_dropped(env, store, monkeypatch):
    """履歴が長いスレッドでは、新しいほうから読み、落とした分があることをプロンプトに書く。"""
    from kei_agent import assistant as mod

    assistant, slack, claude, _ = env
    monkeypatch.setattr(mod, "HISTORY_PAGE", 2)
    monkeypatch.setattr(mod, "HISTORY_MAX_MESSAGES", 4)
    store.upsert_thread("C1", "10.1", "vlm", "lost-session")
    pages = [
        ([{"ts": "10.1", "user": "UME", "text": "いちばん古い話"},
          {"ts": "10.2", "user": "UME", "text": "その次"}], "c1"),
        ([{"ts": "10.3", "user": "UME", "text": "三つめ"},
          {"ts": "10.4", "user": "UME", "text": "四つめ"}], "c2"),
        ([{"ts": "10.5", "user": "UME", "text": "五つめ"},
          {"ts": "10.6", "user": "UME", "text": "いちばん新しい話"}], ""),
    ]
    seen_cursors = []

    async def replies(**kw):
        seen_cursors.append(kw.get("cursor"))
        messages, cursor = pages[len(seen_cursors) - 1]
        return {"messages": messages, "response_metadata": {"next_cursor": cursor}}

    monkeypatch.setattr(slack, "conversations_replies", replies)
    claude.behaviors = [
        {"is_error": True, "text": "", "errors": ["No conversation found with session ID: lost-session"]},
        {"session_id": "sess-new"},
    ]

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.7", "thread_ts": "10.1", "text": "続き"})
    await settle(assistant)

    assert seen_cursors == [None, "c1", "c2"]
    prompt = claude.calls[1]["prompt"]
    assert "いちばん新しい話" in prompt and "いちばん古い話" not in prompt
    assert "古い投稿 2 件は長すぎるので省いた" in prompt


async def test_job_without_its_expected_file_is_reported_as_unfinished(env, store, config):
    """終了コードが成功でも、できるはずのファイルが無ければ、そう書いて返事待ちにする。"""
    assistant, slack, claude, _ = env
    cwd = config.research_root / "vlm"
    cwd.mkdir(parents=True, exist_ok=True)
    store.upsert_thread("C1", "10.1", "vlm", "sess-1")
    job = store.add_job("r1", "C1", "10.1", str(cwd), "sweep", "scripts/sweep.py", status="queued",
                        expects=["outputs/sweep.csv"])
    store.update_job(job.id, status="succeeded", finished_at=time.time())

    await assistant.poll_jobs()
    await settle(assistant)

    posted = [t for t in slack.texts() if t and t.startswith("🧪")]
    assert "outputs/sweep.csv ができていない" in posted[0]
    assert "無いか空" in claude.calls[0]["prompt"]
    assert store.get_thread("C1", "10.1")["awaiting_since"] is not None


async def test_new_outputs_are_uploaded(env, config):
    assistant, slack, claude, _ = env
    old = config.research_root / "vlm" / "outputs"
    old.mkdir(parents=True)
    (old / "old.png").write_bytes(b"old")
    claude.behaviors = [{"side_effect": lambda cwd: (cwd / "outputs" / "fig.png").write_bytes(b"png")}]

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図"})
    await settle(assistant)

    upload, = [kw for name, kw in slack.calls if name == "files_upload_v2"]
    assert [f["filename"] for f in upload["file_uploads"]] == ["fig.png"]
    assert upload["thread_ts"] == "10.1"


async def test_error_result_is_reported(env):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"is_error": True, "text": "", "errors": ["rate limited"]}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)
    assert "⚠️ エラーで止まっちゃった: rate limited" in slack.texts()
    assert slack.texts()[-1] == "<@UME> 返事がほしいよ"
    # 止まったときは ✅ ではなく ⚠️ をつける
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "warning"}) in slack.calls
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "white_check_mark"}) not in slack.calls


async def test_narration_goes_to_the_status_not_the_answer(env):
    """途中の独り言は入力欄の下だけに出し、本文は最後のまとめにする。同じ話が2回並ばない。"""
    assistant, slack, claude, _ = env
    claude.behaviors = [{"steps": [("text", "了解、進めるね。CLAUDE.md に書いておく。"), ("tool", "Edit: CLAUDE.md")],
                         "text": "了解したよ。CLAUDE.md に書いておいた。"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)

    assert slack.streamed() == ["了解したよ。CLAUDE.md に書いておいた。"]
    # 独り言 → 道具 → まとめ。まとめは投稿すると Slack が消すので、残るのは返事の本文だけ
    assert slack.thinking() == ["考え中…", "了解、進めるね。CLAUDE.md に書いておく。", "Edit: CLAUDE.md",
                                "了解したよ。CLAUDE.md に書いておいた。"]
    assert slack.tasks() == []


async def test_run_uses_domains_allowed_for_the_theme(env, store, monkeypatch):
    assistant, slack, claude, _ = env
    from kei_agent import settings
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    seen = []
    original = runner.run_claude

    async def spy(config, ws, *args, **kwargs):
        seen.append(ws.allowed_domains)
        return await original(config, ws, *args, **kwargs)

    monkeypatch.setattr(runner, "run_claude", spy)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)
    assert seen == [("zenodo.org",)]


async def test_answer_without_tools_is_still_streamed(env):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"steps": [], "text": "こんにちは"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)
    assert slack.streamed() == ["こんにちは"] and slack.tasks() == []
    assert "こんにちは" not in slack.texts()


async def test_session_is_suspended_while_awaiting_answer(env):
    """依頼者の判断を待つときは、Slack 側でも「返事待ち」に見せる。"""
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "どちらにしますか\n❓ 確認: A と B のどちらにしますか"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)
    assert slack.statuses() == ["processing", "suspended"]


async def test_improve_channel_records_backlog_in_research_data(env, config):
    assistant, slack, claude, pueue = env

    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    backlog = config.backlog_path.read_text()
    assert "#kei-agent" in backlog and "経過をもっと細かく" in backlog
    # 要望を記録したうえで、直し方の案を考える（書けるのは一時ディレクトリだけ）
    assert claude.calls[0]["cwd"] == config.state_dir / "improve" / "20.1"
    assert "https://example.slack.com/archives/C9/p201" in backlog
    assert slack.texts()[-1] == f"要望を `{config.backlog_path}` に記録したよ。"


async def test_thread_broadcast_reply_continues_thread(env, store):
    assistant, slack, claude, _ = env
    store.upsert_thread("C1", "10.1", "vlm", "s")
    store.set_prompt_version("C1", "10.1", runner.system_prompt_version(assistant.config))
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1",
                                "subtype": "thread_broadcast", "text": "チャンネルにも送った返信"})
    await settle(assistant)
    assert claude.calls[0]["prompt"] == "チャンネルにも送った返信"


async def test_channel_rename_forgets_cached_name(env):
    assistant, slack, claude, _ = env
    assert await assistant.channel_name("C1") == "vlm"
    slack.channels["C1"] = "vlm-counting"
    await assistant.on_channel_rename({"channel": {"id": "C1", "name": "vlm-counting"}})
    assert await assistant.channel_name("C1") == "vlm-counting"


async def test_upload_failure_does_not_hide_result(env, config, monkeypatch):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"side_effect": lambda cwd: (cwd / "outputs" / "fig.png").write_bytes(b"png")}]

    async def broken_upload(**kw):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(slack, "files_upload_v2", broken_upload)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図"})
    await settle(assistant)

    assert slack.streamed() == ["結果です"]
    texts = slack.texts()
    assert texts[-1].startswith("⚠️ `outputs/` のファイルを添付できなかったよ")
    assert not any("内部エラー" in t for t in texts)


async def test_same_thread_requests_run_one_at_a_time(env, monkeypatch):
    """同じスレッドの依頼は1つずつ動かす。

    1件目が終わった直後に3件目が来ても、2件目と同時には走らせない。
    同じスレッド = 同じセッションなので、2本の claude が同時に動くと会話が混ざる。
    """
    assistant, slack, claude, _ = env
    gates = [asyncio.Event() for _ in range(3)]
    state = {"running": 0, "peak": 0, "calls": 0}

    async def gated(config, ws, prompt, session_id, channel, thread_ts, on_activity=None, on_text=None):
        i = state["calls"]
        state["calls"] += 1
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await gates[i].wait()
        state["running"] -= 1
        return runner.RunResult(session_id="sess-1", text="結果です", is_error=False, errors=[])

    async def pump(n=20):
        for _ in range(n):
            await asyncio.sleep(0)

    monkeypatch.setattr(runner, "run_claude", gated)
    # 1件目を動かし、2件目をそのうしろに並ばせる
    for ts in ("10.1", "10.2"):
        await assistant.on_mention({"channel": "C1", "user": "UME", "ts": ts, "thread_ts": "10.1", "text": "<@UBOT> x"})
    await pump()
    assert state["calls"] == 1

    # 1件目を終わらせる。2件目が動き出したところで、3件目を出す
    gates[0].set()
    await pump()
    assert state["calls"] == 2
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.3", "thread_ts": "10.1", "text": "<@UBOT> x"})
    await pump()

    assert state["peak"] == 1
    assert state["calls"] == 2  # 3件目は2件目の終わりを待っている
    for g in gates:
        g.set()
    await settle(assistant)
    assert state["calls"] == 3 and state["peak"] == 1


async def test_thread_lock_is_kept_for_later_requests(env):
    """スレッドのロックは捨てずに残す。捨てると、あとから来た依頼が別のロックを取ってしまう。"""
    assistant, slack, claude, _ = env
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)
    first = assistant.thread_locks[("C1", "10.1")]

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "<@UBOT> y"})
    await settle(assistant)
    assert assistant.thread_locks[("C1", "10.1")] is first


async def test_overview_channel_runs_in_overview_dir(env, config):
    assistant, slack, claude, _ = env
    await assistant.on_mention({"channel": "C5", "user": "UME", "ts": "30.1", "text": "<@UBOT> 全体を見て"})
    await settle(assistant)
    assert claude.calls[0]["cwd"] == config.overview_dir


async def test_job_submitted_during_run_then_resumed_when_finished(env, config, store):
    assistant, slack, claude, pueue = env

    def submit_job(cwd: Path):
        (cwd / "scripts").mkdir(exist_ok=True)
        (cwd / "scripts" / "sweep.py").write_text("print(1)")
        write_request(cwd, action="submit", request_id="req-1", channel="C1", thread_ts="10.1",
                      name="sweep", script="scripts/sweep.py", args=["--n", "50"])

    def job_writes_output(cwd: Path):
        (cwd / "outputs" / "result.csv").write_text("a,b\n")

    claude.behaviors = [{"side_effect": submit_job, "text": "ジョブを投入しました"}, {"text": "集計しました"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 回して"})
    await settle(assistant)

    assert len(pueue.added) == 1
    assert "🧪 ジョブ 1「sweep」を投入したよ: `scripts/sweep.py --n 50`" in slack.texts()
    # ジョブが走っている間は、スレッドを作業中のままにしておく
    assert slack.statuses()[-1] == "processing"

    await assistant.poll_jobs()  # まだ終わっていない
    await settle(assistant)
    assert len(claude.calls) == 1

    job_writes_output(config.research_root / "vlm")
    pueue.task_status[0] = {"status": {"Done": {
        "start": "2026-09-17T10:00:00+09:00", "end": "2026-09-17T10:10:00+09:00", "result": "Success"}}}
    await assistant.poll_jobs()
    await settle(assistant)

    resumed = claude.calls[1]
    assert resumed["session_id"] == "sess-1" and resumed["thread_ts"] == "10.1"
    assert "ジョブ 1「sweep」が終わりました" in resumed["prompt"] and "10分0秒" in resumed["prompt"]
    assert "🧪 ジョブ 1「sweep」が終わったよ（成功）。結果を見てみるね" in slack.texts()
    assert slack.streamed()[-1] == "集計しました"
    assert slack.statuses()[-1] == "active"  # 報告し終えたら、次の依頼待ちに戻す
    upload, = [kw for name, kw in slack.calls if name == "files_upload_v2"]
    assert [f["filename"] for f in upload["file_uploads"]] == ["result.csv"]

    await assistant.poll_jobs()  # 二度は報告しない
    await settle(assistant)
    assert len(claude.calls) == 2


async def test_same_thread_runs_one_at_a_time(env, monkeypatch):
    assistant, slack, claude, _ = env
    running = 0
    max_running = 0

    async def slow(*args, **kwargs):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        return await claude(*args, **kwargs)

    monkeypatch.setattr(runner, "run_claude", slow)
    assistant.store.upsert_thread("C1", "10.1", "vlm", "s")
    assistant.store.set_prompt_version("C1", "10.1", runner.system_prompt_version(assistant.config))
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "a"})
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.3", "thread_ts": "10.1", "text": "b"})
    await settle(assistant)
    assert max_running == 1 and [c["prompt"] for c in claude.calls] == ["a", "b"]


def test_split_text_prefers_paragraphs():
    text = ("a" * 60 + "\n\n") * 5
    chunks = split_text(text, limit=150)
    assert all(len(c) <= 150 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_history_prompt_skips_progress_and_excluded():
    prompt = history_prompt(
        [{"ts": "1", "user": "U", "text": "依頼"}, {"ts": "2", "bot_id": "B", "text": "✅ 3秒 作業しました"},
         {"ts": "3", "user": "U", "text": "新しい依頼"}],
        "UBOT", "新しい依頼", exclude_ts="3",
    )
    assert "依頼者: 依頼" in prompt and "作業しました" not in prompt and prompt.count("新しい依頼") == 1


async def test_job_loop_notifies_once_while_failing(env, config, monkeypatch):
    assistant, slack, claude, pueue = env
    polls = 0

    async def broken_poll():
        nonlocal polls
        polls += 1
        if polls >= 4:
            raise asyncio.CancelledError
        raise RuntimeError("pueued is not running")

    monkeypatch.setattr(assistant, "poll_jobs", broken_poll)
    object.__setattr__(assistant.config, "job_poll_seconds", 0)  # Config は frozen なので直接書き換える
    with pytest.raises(asyncio.CancelledError):
        await assistant.job_loop()
    notices = [t for t in slack.texts() if "pueue" in t]
    assert len(notices) == 1 and slack.posted()[-1]["channel"] == "C9"


async def test_crash_before_the_run_is_reported_to_the_thread(env, monkeypatch):
    """作業用ディレクトリを作れないときなどに、👀 がついたまま黙って終わらない。"""
    assistant, slack, claude, _ = env
    from kei_agent import themes

    def boom(ws):
        raise RuntimeError("ディスクが一杯です")

    monkeypatch.setattr(themes, "ensure_workspace", boom)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    texts = slack.texts()
    assert any("依頼の処理が落ちました" in t and "ディスクが一杯です" in t for t in texts)
    assert claude.calls == []
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "warning"}) in slack.calls


async def test_shows_thinking_text_while_working(env):
    """作業中は、Slack の状態欄に「考え中…」を出す。"""
    assistant, slack, claude, _ = env

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    assert slack.thinking()[0] == "考え中…"


async def test_keeps_working_when_the_thinking_text_api_is_unavailable(env, monkeypatch):
    """文言を指定する API が断られても、状態の切り替えと返事は今までどおり。"""
    assistant, slack, claude, _ = env
    from slack_sdk.errors import SlackApiError

    async def unavailable(**kw):
        raise SlackApiError("missing_scope", {"ok": False})

    monkeypatch.setattr(slack, "assistant_threads_setStatus", unavailable, raising=False)

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    assert slack.statuses() == ["processing", "active"]


async def test_falls_back_to_a_plain_post_when_the_new_slack_api_is_unavailable(env, monkeypatch):
    """ステータスと流し見せが使えないワークスペースでは、今までどおり投稿する。"""
    assistant, slack, claude, _ = env
    from slack_sdk.errors import SlackApiError

    async def unavailable(**kw):
        raise SlackApiError("method_not_supported_for_channel_type", {"ok": False})

    monkeypatch.setattr(slack, "agents_sessions_setStatus", unavailable, raising=False)
    monkeypatch.setattr(slack, "chat_startStream", unavailable, raising=False)

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    assert slack.posted()[-1] == {"channel": "C1", "thread_ts": "10.1", "markdown_text": "結果です"}
    assert slack.streamed() == []


async def test_publish_records_the_thread_even_without_a_session(env, config, store):
    """claude がセッションを作る前に落ちた日でも、そのスレッドへの返信に反応できるようにする。"""
    assistant, slack, claude, _ = env
    from kei_agent import themes

    ws = themes.resolve(config, "research-overview")
    themes.ensure_workspace(ws)
    result = runner.RunResult(session_id=None, text="", is_error=True, errors=["起動できません"])

    thread_ts = await assistant.publish("C5", "research-overview", ws, "🌙 振り返りの材料", result)

    assert store.get_thread("C5", thread_ts) is not None


def test_message_text_reads_messages_posted_as_markdown():
    """流して見せた返事や markdown_text の投稿は、text が空で blocks に入る。"""
    from kei_agent.slack_text import message_text

    assert message_text({"text": "ふつうの投稿"}) == "ふつうの投稿"
    assert message_text({"text": "", "blocks": [{"type": "markdown", "text": "流した返事"}]}) == "流した返事"
    assert message_text({"blocks": [{"type": "rich_text", "elements": [
        {"elements": [{"type": "text", "text": "書き込み"}]}]}]}) == "書き込み"


def test_theme_runs_remembers_overlap():
    """outputs/ はテーマで共通なので、重なって動いたことを覚えておく。"""
    from kei_agent.assistant import ThemeRuns

    runs = ThemeRuns()
    runs.begin("vlm", "1.1")
    assert runs.end("vlm", "1.1") is False  # 1つだけなら混ざらない

    runs.begin("vlm", "2.1")
    runs.begin("vlm", "2.2")  # 重なった
    assert runs.end("vlm", "2.2") is True
    assert runs.end("vlm", "2.1") is True
    assert runs.running == {} and runs.overlapped == set()

    runs.begin("vlm", "3.1")
    runs.begin("other", "3.2")  # テーマが違えば混ざらない
    assert runs.end("vlm", "3.1") is False and runs.end("other", "3.2") is False


async def test_overlapping_threads_are_warned_when_files_are_attached(env, config, monkeypatch):
    """別のスレッドの図が混ざりうることを、黙って隠さない。"""
    assistant, slack, claude, _ = env

    def make_figure(cwd):
        (cwd / "outputs").mkdir(exist_ok=True)
        (cwd / "outputs" / "plot.png").write_bytes(b"x")

    claude.behaviors = [{"side_effect": make_figure}]
    # 先に別のスレッドが動いている状態にする
    assistant.theme_runs.begin("vlm", "99.1")

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    assert any("別のスレッドの図が混ざっているかもしれない" in t for t in slack.texts())


# 接続先の申し出（🔒 接続:）

def _button_action(slack, name):
    """投稿されたボタンのうち、action_id が name のもの。"""
    for _, kw in reversed(slack.calls):
        for block in kw.get("blocks") or []:
            for el in block.get("elements", []):
                if el.get("action_id") == name:
                    return el, kw
    raise AssertionError(f"{name} のボタンがありません")


def _press(el, user="UME", message_ts="99.1"):
    return {"user": {"id": user}, "actions": [{"action_id": el["action_id"], "value": el["value"]}],
            "container": {"channel_id": "C1", "message_ts": message_ts}, "channel": {"id": "C1"}}


async def test_connect_request_becomes_buttons_and_resumes_when_allowed(env, store):
    assistant, slack, claude, _ = env
    from kei_agent import settings
    claude.behaviors = [
        {"text": "Zenodo につながらなかった。\n🔒 接続: zenodo.org（CASTELLA の特徴量を落とすため）"},
        {"text": "落とせたよ"},
    ]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    allow, post = _button_action(slack, "kei_agent_domain_allow")
    assert post["thread_ts"] == "10.1" and "zenodo.org" in post["text"]
    assert slack.statuses()[-1] == "suspended"  # 返事待ちに見せる

    await assistant.on_domain_action(_press(allow))
    await settle(assistant)

    assert settings.theme_domains(store, "vlm") == ["zenodo.org"]
    update = [kw for name, kw in slack.calls if name == "chat_update"][-1]
    assert "許可しました" in update["text"] and not any(b.get("type") == "actions" for b in update["blocks"])
    resumed = claude.calls[1]
    assert resumed["session_id"] == "sess-1" and "zenodo.org" in resumed["prompt"] and "許可" in resumed["prompt"]


async def test_connect_request_denied_resumes_with_that_news(env, store):
    assistant, slack, claude, _ = env
    from kei_agent import settings
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）"}, {"text": "別の入手先を探すね"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    deny, _ = _button_action(slack, "kei_agent_domain_deny")
    await assistant.on_domain_action(_press(deny))
    await settle(assistant)

    assert settings.theme_domains(store, "vlm") == []
    assert "断" in claude.calls[1]["prompt"]


async def test_only_the_allowed_user_can_press(env, store):
    assistant, slack, claude, _ = env
    from kei_agent import settings
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    allow, _ = _button_action(slack, "kei_agent_domain_allow")
    await assistant.on_domain_action(_press(allow, user="USOMEONE"))
    await settle(assistant)
    assert settings.theme_domains(store, "vlm") == [] and len(claude.calls) == 1


async def test_several_requests_resume_once_after_all_answered(env, store):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）\n🔒 接続: huggingface.co（重み）"}, {"text": "続けたよ"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    buttons = [el for _, kw in slack.calls for b in kw.get("blocks") or [] for el in b.get("elements", [])
               if el.get("action_id") == "kei_agent_domain_allow"]
    assert len(buttons) == 2

    await assistant.on_domain_action(_press(buttons[0]))
    await settle(assistant)
    assert len(claude.calls) == 1  # もう1つに答えるまで待つ

    await assistant.on_domain_action(_press(buttons[1]))
    await assistant.on_domain_action(_press(buttons[1]))  # 2度押し
    await settle(assistant)
    assert len(claude.calls) == 2
    assert "zenodo.org" in claude.calls[1]["prompt"] and "huggingface.co" in claude.calls[1]["prompt"]


async def test_connect_request_for_an_allowed_domain_asks_nothing(env, store):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "🔒 接続: export.arxiv.org（論文）"}]  # config.toml で許可済み
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 探して"})
    await settle(assistant)
    assert not any(kw.get("blocks") for _, kw in slack.calls if "blocks" in kw and kw.get("text", "").startswith("🔒"))


async def test_domains_asked_through_the_bash_tool_also_become_buttons(env, monkeypatch):
    """Claude が 🔒 の行を書かず、Bash の allowed_domains で頼んだときも、ボタンを出す。"""
    assistant, slack, claude, _ = env
    original = runner.run_claude

    async def asks_with_tool(config, ws, *args, **kwargs):
        result = await original(config, ws, *args, **kwargs)
        result.requested_domains = [("huggingface.co", "重みを落とす")]
        return result

    monkeypatch.setattr(runner, "run_claude", asks_with_tool)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)
    allow, post = _button_action(slack, "kei_agent_domain_allow")
    assert "huggingface.co" in post["text"] and "重みを落とす" in post["text"]


async def test_updated_rules_are_passed_to_a_resumed_session_once(env, monkeypatch):
    """--resume では、会話を始めたときのシステムプロンプトが使われ続ける。
    決まりを変えたら、次の依頼のときに1回だけ本文として渡す。"""
    assistant, slack, claude, _ = env
    version = {"v": "v1"}
    monkeypatch.setattr(runner, "system_prompt_version", lambda config: version["v"])
    monkeypatch.setattr(runner, "system_prompt_text", lambda config: "新しい決まり")

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 始めて"})
    await settle(assistant)
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "続き"})
    await settle(assistant)
    assert "新しい決まり" not in claude.calls[0]["prompt"] and "新しい決まり" not in claude.calls[1]["prompt"]

    version["v"] = "v2"
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.3", "thread_ts": "10.1", "text": "もう一度"})
    await settle(assistant)
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.4", "thread_ts": "10.1", "text": "さらに"})
    await settle(assistant)
    assert "新しい決まり" in claude.calls[2]["prompt"] and claude.calls[2]["prompt"].endswith("もう一度")
    assert "新しい決まり" not in claude.calls[3]["prompt"]


# 契約の上限（Claude AI usage limit）

async def test_usage_limit_is_retried_after_it_resets(env, store, monkeypatch):
    """上限に達したら、明ける時刻を伝えて、そのあと自動でやり直す。"""
    assistant, slack, claude, _ = env
    reset = time.time() + 3600
    claude.behaviors = [{"is_error": True, "text": f"Claude AI usage limit reached|{int(reset)}", "errors": []},
                        {"text": "やり直したよ"}]

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    texts = "\n".join(slack.texts())
    assert "上限" in texts and "やり直す" in texts
    assert "エラーで止まっちゃった" not in texts        # ふつうのエラーとしては出さない
    assert assistant.limited_until > time.time()
    assert store.due_deferred("request", time.time()) == []      # まだ明けていない

    deferred = store.due_deferred("request", reset + 120)
    assert deferred and deferred[0][1]["text"] == "図を作って"

    await assistant.retry_deferred(now=reset + 120)
    await settle(assistant)
    assert claude.calls[1]["prompt"].endswith("続きの依頼:\n図を作って") and "やり直したよ" in slack.streamed()
    assert "上限が明けたので" not in "\n".join(slack.texts())
    assert store.due_deferred("request", reset + 200) == []       # 二度はやり直さない


async def test_usage_limit_without_a_reset_time_waits_a_while(env, store):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"is_error": True, "text": "Claude AI usage limit reached", "errors": []}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)
    assert time.time() + 60 < assistant.limited_until <= time.time() + assistant.LIMIT_FALLBACK_SECONDS + 5


async def test_next_request_after_an_error_carries_the_stalled_request(env, store):
    assistant, slack, claude, _ = env
    slack.replies = [
        {"ts": "10.1", "user": "UME", "text": "<@UBOT> 図を作って"},
        {"ts": "10.5", "user": "UME", "text": "続けて"},
    ]
    claude.behaviors = [{"is_error": True, "text": "", "errors": ["boom"], "session_id": "s1"},
                        {"session_id": "s2"}, {"session_id": "s3"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.5", "thread_ts": "10.1", "text": "続けて"})
    await settle(assistant)

    second = claude.calls[1]
    assert second["session_id"] is None
    assert "止まった依頼" in second["prompt"] and "図を作って" in second["prompt"]
    assert second["prompt"].endswith("続きの依頼:\n続けて")
    assert store.get_thread("C1", "10.1")["stalled_request"] is None    # 成功したので忘れる

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.6", "thread_ts": "10.1", "text": "次"})
    await settle(assistant)
    assert claude.calls[2]["session_id"] == "s2" and claude.calls[2]["prompt"] == "次"


# Slack の外からの依頼（声のレイヤ。docs/design.md の12章）

async def test_ask_from_outside_starts_a_thread_and_runs(env, config):
    assistant, slack, claude, _ = env
    from kei_agent import ask
    ask.write_ask(config, "vlm", "条件ごとの精度を集計して")

    await assistant.handle_asks()
    await settle(assistant)

    posted = [kw for name, kw in slack.calls if name == "chat_postMessage"]
    assert "🎤 声からの依頼" in posted[0]["text"] and "条件ごとの精度" in posted[0]["text"]
    assert claude.calls[0]["prompt"] == "条件ごとの精度を集計して"
    assert claude.calls[0]["cwd"] == config.research_root / "vlm"
    assert ask.pending_asks(config) == []      # 拾ったら消す


async def test_ask_from_outside_is_retried_when_slack_post_fails(env, config, monkeypatch):
    """Slack へのスレッド作成が一時失敗しても、依頼を次回へ残す。"""
    assistant, slack, _, _ = env
    from kei_agent import ask
    path = ask.write_ask(config, "vlm", "集計して")
    monkeypatch.setattr(slack, "chat_postMessage", AsyncMock(side_effect=RuntimeError("temporary")))

    with pytest.raises(RuntimeError, match="temporary"):
        await assistant.handle_asks()

    assert path.exists()
    assert ask.pending_asks(config)[0][1]["text"] == "集計して"


async def test_ask_from_outside_is_retried_when_submit_fails(env, config, monkeypatch):
    """依頼の受付を再試行しても、Slack スレッドは重複させない。"""
    assistant, slack, _, _ = env
    from kei_agent import ask
    path = ask.write_ask(config, "vlm", "集計して")
    submit = AsyncMock(side_effect=[RuntimeError("temporary"), None])
    monkeypatch.setattr(assistant, "submit", submit)

    with pytest.raises(RuntimeError, match="temporary"):
        await assistant.handle_asks()

    assert path.exists()
    persisted = ask.pending_asks(config)[0][1]
    assert persisted["text"] == "集計して"
    assert persisted["thread_ts"] == "1001.000"

    await assistant.handle_asks()

    assert len(slack.posted()) == 1
    assert submit.await_count == 2
    assert submit.await_args_list[0].args[0].thread_ts == persisted["thread_ts"]
    assert submit.await_args_list[1].args[0].thread_ts == persisted["thread_ts"]
    assert ask.pending_asks(config) == []


async def test_ask_loop_recovers_interrupted_asks_only_before_polling(env, config, monkeypatch):
    """再起動時の回収は1回だけ行い、通常の poll では所有権を保つ。"""
    assistant, _, _, _ = env
    from kei_agent import ask
    events = []

    monkeypatch.setattr(ask, "recover_asks", lambda actual: events.append(("recover", actual)))

    async def handle_asks():
        events.append(("handle", config))
        if len(events) == 3:
            raise asyncio.CancelledError

    async def no_sleep(_):
        return None

    monkeypatch.setattr(assistant, "handle_asks", handle_asks)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await assistant.ask_loop()

    assert events == [("recover", config), ("handle", config), ("handle", config)]


async def test_note_from_outside_is_only_recorded(env, config):
    """決まったことは、作業させずにスレッドに残すだけ。"""
    assistant, slack, claude, _ = env
    from kei_agent import ask
    ask.write_ask(config, "vlm", "4条件の比較で進める", kind="note")

    await assistant.handle_asks()
    await settle(assistant)

    assert "📌 声で決まったこと" in "\n".join(slack.texts())
    assert claude.calls == []


async def test_note_from_outside_is_not_posted_again_when_completion_is_retried(env, config, monkeypatch):
    """投稿後の完了に失敗しても、保存済みの Slack スレッドを再利用する。"""
    assistant, slack, claude, _ = env
    from kei_agent import ask
    path = ask.write_ask(config, "vlm", "4条件の比較で進める", kind="note")
    real_complete = ask.complete_ask
    attempts = 0

    def complete_once_retried(item):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        real_complete(item)

    monkeypatch.setattr(ask, "complete_ask", complete_once_retried)

    with pytest.raises(RuntimeError, match="temporary"):
        await assistant.handle_asks()

    assert path.exists()
    assert ask.pending_asks(config)[0][1]["thread_ts"] == "1001.000"

    await assistant.handle_asks()

    notes = [text for text in slack.texts() if text.startswith("📌 声で決まったこと")]
    assert len(notes) == 1
    assert claude.calls == []
    assert ask.pending_asks(config) == []


async def test_ask_for_an_unknown_theme_is_reported(env, config):
    assistant, slack, claude, _ = env
    from kei_agent import ask
    ask.write_ask(config, "nothere", "何かして")

    await assistant.handle_asks()
    await settle(assistant)

    assert claude.calls == []
    assert "渡せませんでした" in "\n".join(slack.texts())
    assert ask.pending_asks(config) == []


def _mentions(slack):
    return [t for t in slack.texts() if t.startswith("<@UME>")]


async def test_owner_is_mentioned_when_waiting_for_an_answer(env):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "途中まで進めた\n❓ 確認: Bも含める？"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 進めて"})
    await settle(assistant)
    assert len(_mentions(slack)) == 1


async def test_owner_is_mentioned_when_connect_buttons_are_shown(env):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "つながらない\n🔒 接続: zenodo.org（特徴量のため）"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)
    assert len(_mentions(slack)) == 1


async def test_short_finished_run_does_not_mention(env):
    assistant, slack, claude, _ = env
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> こんにちは"})
    await settle(assistant)
    assert _mentions(slack) == []


async def test_long_finished_run_mentions(env, monkeypatch):
    from kei_agent import assistant as mod
    assistant, slack, claude, _ = env
    monkeypatch.setattr(mod, "NOTIFY_AFTER_SECONDS", -1)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 集計して"})
    await settle(assistant)
    assert len(_mentions(slack)) == 1 and "終わった" in _mentions(slack)[0]


# 再起動で途中で止まった依頼のやり直し


async def test_request_is_recorded_while_it_runs(env, store, monkeypatch):
    """強制終了されても拾えるよう、claude が動いている間は控えが残っている。"""
    assistant, _, claude, _ = env
    from kei_agent import runner
    seen = []

    async def watching(*args, **kw):
        seen.append([p["text"] for _, p in store.interrupted_requests()])
        return await claude(*args, **kw)

    monkeypatch.setattr(runner, "run_claude", watching)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    assert seen == [["図を作って"]]
    assert store.interrupted_requests() == []  # 終わったら消える


async def test_cancelled_request_stays_recorded_for_the_next_start(env, store, monkeypatch):
    """終了処理でキャンセルされた依頼は、次の起動でやり直せるよう控えを残す。"""
    from kei_agent import themes

    assistant, _, _, _ = env
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()

    monkeypatch.setattr(runner, "run_claude", slow)
    req = Request("C1", "vlm", "10.1", "10.1", "図を作って")
    ws = themes.resolve(assistant.config, "vlm")
    themes.ensure_workspace(ws)
    task = asyncio.create_task(assistant.run(req, ws))
    for _ in range(20):
        if store.interrupted_requests():
            break
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [payload["text"] for _, payload in store.interrupted_requests()] == ["図を作って"]
    assert assistant.theme_runs.running == {}
    run = store.conn.execute("SELECT ended_at, is_error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["ended_at"] is not None and run["is_error"] == 1


async def test_interrupted_request_is_resumed_on_start(env, store):
    """強制終了で控えが残ったまま起動したときは、断って続きからやり直す。"""
    assistant, slack, claude, _ = env
    store.start_in_flight(Request("C1", "vlm", "10.1", "10.1", "図を作って").to_payload())

    assert await assistant.resume_interrupted() == 1
    await settle(assistant)

    assert store.interrupted_requests() == []
    assert "途中で止まっちゃった" in slack.texts()[0]
    assert "図を作って" in claude.calls[0]["prompt"] and "再起動で途中で止まりました" in claude.calls[0]["prompt"]
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "white_check_mark"}) in slack.calls


async def test_finished_request_leaves_nothing_to_resume(env, store):
    assistant, slack, claude, _ = env
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)
    assert store.interrupted_requests() == []
    assert await assistant.resume_interrupted() == 0


async def test_open_runs_are_closed_on_start(env, store):
    assistant, _, _, _ = env
    run_id = store.start_run("C1", "10.1", "vlm", "message")
    await assistant.resume_interrupted()
    row = store.conn.execute("SELECT ended_at, is_error FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["ended_at"] is not None and row["is_error"] == 1


async def test_second_request_in_a_busy_thread_says_it_will_wait(env, monkeypatch):
    """同じスレッドで続けて頼まれたら、黙って待たせずに一言返す。"""
    assistant, slack, claude, _ = env
    gate = asyncio.Event()

    async def slow(config, ws, prompt, session_id, channel, thread_ts, on_activity=None, on_text=None):
        await gate.wait()
        return await claude(config, ws, prompt, session_id, channel, thread_ts, on_activity, on_text)

    monkeypatch.setattr(runner, "run_claude", slow)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 集計して"})
    await asyncio.sleep(0)
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "図も"})
    await asyncio.sleep(0)

    assert "いま前の作業をしているから" in slack.texts()[0]
    gate.set()
    await settle(assistant)
    # 順番に処理する（2件とも動く）
    assert [c["prompt"] for c in claude.calls] == ["集計して", "図も"]
    assert sum("いま前の作業" in (t or "") for t in slack.texts()) == 1


async def test_status_inquiry_during_busy_thread_answers_immediately_without_queuing(env, monkeypatch):
    """処理中に「今どんな感じ？」と聞かれたら、新しい依頼としてキューに積まず、その場で状況を返す。"""
    assistant, slack, claude, _ = env
    gate = asyncio.Event()

    async def slow(config, ws, prompt, session_id, channel, thread_ts, on_activity=None, on_text=None):
        await gate.wait()
        return await claude(config, ws, prompt, session_id, channel, thread_ts, on_activity, on_text)

    monkeypatch.setattr(runner, "run_claude", slow)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 集計して"})
    await asyncio.sleep(0)
    await assistant.on_message(
        {"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "今どんな感じ？"})
    await asyncio.sleep(0)

    # 進み具合を尋ねる一言には、claude を待たせずにその場で状況を返す
    assert any("処理してる" in t for t in slack.texts())
    assert ("reactions_remove", {"channel": "C1", "timestamp": "10.2", "name": "eyes"}) in slack.calls
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.2", "name": "white_check_mark"}) in slack.calls

    gate.set()
    await settle(assistant)
    # 進み具合を尋ねる一言は claude に渡らない。もとの依頼だけが処理される
    assert [c["prompt"] for c in claude.calls] == ["集計して"]


async def test_saying_continue_cancels_the_scheduled_retry(env, store):
    """上限で止まった依頼は明けたらやり直すが、先に依頼者が続けたら、そちらを優先して二重に走らせない。"""
    assistant, slack, claude, _ = env
    store.upsert_thread("C1", "10.1", "vlm", "sess-1")
    claude.behaviors = [{"is_error": True, "text": "You've hit your session limit · resets 6:30pm (Asia/Tokyo)"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 集計して"})
    await settle(assistant)

    assert any("上限に達したみたい" in (t or "") for t in slack.texts())
    assert len(store.pending_deferred("request")) == 1
    assert store.get_thread("C1", "10.1")["stalled_request"] == "集計して"

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "続けて"})
    await settle(assistant)

    assert store.pending_deferred("request") == []
    assert any("自動のやり直しをやめて" in (t or "") for t in slack.texts())
    # 止まった依頼を文脈として渡す（「続けて」だけでは何を続けるか分からない）
    assert "集計して" in claude.calls[1]["prompt"]
    await assistant.retry_deferred(now=2 ** 31)
    await settle(assistant)
    assert len(claude.calls) == 2


# 大学のチャンネル（#20_course）


async def test_course_channel_asks_the_university_agent(env):
    """大学の依頼は claude を動かさず、大学エージェントに取り次いで返事をそのまま出す。"""
    from kei_agent import a2a

    assistant, slack, claude, _ = env
    slack.channels["C7"] = "20_course"
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def ask(self, skill, text="", params=None):
            asked.append((skill, params))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text="新しい課題 2 件")

    assistant.agents["course"] = _Agent()

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "11.1", "text": "<@UBOT> 課題を取り込んで"})
    await settle(assistant)

    assert asked == [("sync-assignments", None)]
    assert "新しい課題 2 件" in slack.texts()
    assert claude.calls == []
    assert ("reactions_add", {"channel": "C7", "timestamp": "11.1", "name": "white_check_mark"}) in slack.calls


async def test_course_channel_sends_free_questions_to_ask(env, store):
    """定型に当てはまらない質問は ask に回し、大学エージェント自身の claude が答える。"""
    import json as _json

    from kei_agent import a2a

    assistant, slack, claude, _ = env
    slack.channels["C7"] = "20_course"
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def stream(self, skill, text="", params=None, on_progress=None):
            asked.append((skill, _json.loads(text)))
            if on_progress:
                await on_progress(_json.dumps({"activity": "box_search: 過去問"}))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text="", status_text=_json.dumps({
                "ok": True, "text": "過去問は Box の Personal/過去問 にあるよ", "limit_reset_at": None,
                "cost_usd": 0.01, "data": {"session_id": "course-1"}}))

    assistant.agents["course"] = _Agent()

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "11.2",
                                "text": "<@UBOT> 情報セキュリティBの過去問ある？"})
    await settle(assistant)

    skill, payload = asked[0]
    assert skill == "ask" and payload["prompt"] == "情報セキュリティBの過去問ある？"
    assert payload["session_id"] is None and payload["thread_ts"] == "11.2"
    assert claude.calls == []                      # 本体では claude を動かさない
    assert "box_search: 過去問" in slack.thinking()  # 経過は1行だけに出す
    assert slack.streamed() == ["過去問は Box の Personal/過去問 にあるよ"]
    # 会話の続きは本体が覚える（次の質問は同じ claude の会話につながる）
    assert store.agent_session("C7", "11.2", "course") == "course-1"


async def test_course_thread_takes_replies_without_a_mention(env, store):
    """大学のスレッドでも、メンションなしの返信で続けられる（スレッドを覚えていないと拾えない）。"""
    import json as _json

    from kei_agent import a2a

    assistant, slack, _, _ = env
    slack.channels["C7"] = "20_course"
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def ask(self, skill, text="", params=None):
            asked.append(skill)
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps({
                "ok": True, "text": "締切はないよ", "data": {"items": []},
                "limit_reset_at": None, "cost_usd": None}))

    assistant.agents["course"] = _Agent()

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "12.1",
                                "text": "<@UBOT> 締切を教えて"})
    await settle(assistant)
    assert store.get_thread("C7", "12.1") is not None

    await assistant.on_message({"channel": "C7", "user": "UME", "ts": "12.5",
                                "thread_ts": "12.1", "text": "来週の締切は？"})
    await settle(assistant)

    assert asked == ["list-due", "list-due"]


async def test_overview_thread_keeps_asking_the_agent_that_answered(env, monkeypatch):
    """研究全体のスレッドの続きは、会話の鍵を持たない相手（仕事）でも、同じ相手のまま続ける。"""
    import json as _json

    from kei_agent import a2a, router

    assistant, _, _, _ = env
    asked, picked = [], []

    class _Work:
        base_url = "http://127.0.0.1:8789"

        async def ask(self, skill, text="", params=None):
            asked.append(skill)
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps({
                "ok": True, "text": "予定はないよ", "data": {"items": []},
                "limit_reset_at": None, "cost_usd": None}))

    assistant.agents.clear()
    assistant.agents["work"] = _Work()
    assistant.agent_skills["work"] = [{"id": "list-events", "description": "予定"}]
    assistant.agent_skills_read_at["work"] = time.time()

    async def fake_pick_across(config, by_agent, text):
        picked.append(text)
        return router.Choice(agent="work", skill="list-events")

    async def fake_pick(config, skills, text):
        return router.Choice(skill="list-events")

    monkeypatch.setattr(router, "pick_across", fake_pick_across)
    monkeypatch.setattr(router, "pick", fake_pick)

    await assistant.process(Request("C5", "research-overview", "21.1", None, "今日の会議は？"))
    await assistant.process(Request("C5", "research-overview", "21.1", None, "そのあとは？"))

    assert asked == ["list-events", "list-events"]
    # 2回目は、どのエージェントに聞くかを選び直さない
    assert picked == ["今日の会議は？"]


async def test_course_channel_formats_the_deadlines(env):
    """締切は JSON で返ってくるので、Slack 向けの短い行に組み直して出す。"""
    import json as _json

    from kei_agent import a2a

    assistant, slack, _, _ = env
    slack.channels["C7"] = "20_course"
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def ask(self, skill, text="", params=None):
            asked.append((skill, params))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps({
                "ok": True, "text": "締切 1 件", "limit_reset_at": None, "cost_usd": None,
                "data": {"days": 14, "more": 2,
                         "items": [{"id": "1@moodle", "at": "2026-09-21T17:00:00+09:00",
                                    "course": "プロジェクト研究B", "title": "履修申請フォーム"}]}}))

    assistant.agents["course"] = _Agent()

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "12.1", "text": "<@UBOT> 締切を教えて"})
    await settle(assistant)

    assert asked == [("list-due", None)]
    reply = slack.texts()[-1]
    assert "9/21（月） 17:00 プロジェクト研究B / 履修申請フォーム" in reply
    assert "（ほかに 2 件）" in reply


async def test_course_channel_tells_when_the_agent_is_down(env):
    """大学エージェントにつながらないときは、スレッドに言って `#00_kei-agent` にも知らせる。"""
    from kei_agent import a2a

    assistant, slack, _, _ = env
    slack.channels["C7"] = "20_course"

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def ask(self, skill, text="", params=None):
            raise a2a.A2AError("名刺を読めません（HTTP 502）")

    assistant.agents["course"] = _Agent()

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "12.2", "text": "<@UBOT> 締切を教えて"})
    await settle(assistant)

    assert any("頼めなかった" in (t or "") for t in slack.texts())
    assert any("HTTP 502" in (t or "") for t in slack.texts())
    assert ("reactions_add", {"channel": "C7", "timestamp": "12.2", "name": "warning"}) in slack.calls


async def test_course_channel_waits_out_a_restart_instead_of_failing(env, monkeypatch):
    """入れ替えの最中で一瞬つながらないだけなら、待ってやり直して失敗を見せない。"""
    import json

    from kei_agent import a2a, agents

    assistant, slack, _, _ = env
    slack.channels["C7"] = "20_course"
    monkeypatch.setattr(agents, "RETRY_WAIT", 0)
    tries = []

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def ask(self, skill, text="", params=None):
            tries.append(skill)
            if len(tries) == 1:
                raise a2a.NotReachable("つながりません（http://127.0.0.1:8787）")
            return a2a.TaskResult(state="completed",
                                  text=json.dumps({"ok": True, "text": "締切はないよ", "data": {"due": []}}))

    assistant.agents["course"] = _Agent()

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "12.3", "text": "<@UBOT> 締切を教えて"})
    await settle(assistant)

    assert len(tries) == 2
    assert not any("頼めなかった" in (t or "") for t in slack.texts())
    assert ("reactions_add", {"channel": "C7", "timestamp": "12.3", "name": "warning"}) not in slack.calls


# 研究エージェント（A2A）に実行を任せる


async def test_claude_runs_through_the_research_agent_when_configured(env, store):
    """[a2a] research_url を書くと、claude は研究エージェント経由で動く（本体の中では動かさない）。"""
    import json as _json

    from kei_agent import a2a

    assistant, slack, claude, _ = env
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8788"

        async def stream(self, skill, text="", params=None, on_progress=None):
            asked.append((skill, _json.loads(text)))
            if on_progress:
                await on_progress(_json.dumps({"activity": "Bash: 図を描く"}))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text="", status_text=_json.dumps(
                {"ok": True, "text": "できたよ", "limit_reset_at": None, "cost_usd": None,
                 "data": {"text": "できたよ", "session_id": "sess-7", "is_error": False}}))

    assistant.agents["research"] = _Agent()

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "13.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    assert claude.calls == []
    skill, payload = asked[0]
    assert skill == "run-claude"
    assert payload["channel_name"] == "vlm" and payload["prompt"] == "図を作って"
    assert payload["channel"] == "C1" and payload["thread_ts"] == "13.1"
    # 経過は入力欄の下の1行に出し、返事はいつもどおり流して見せる
    assert "Bash: 図を描く" in slack.thinking()
    assert slack.streamed() == ["できたよ"]
    assert store.get_thread("C1", "13.1")["session_id"] == "sess-7"


# 振り分け係（軽いモデルで、どの仕事かを選ぶ）


def test_router_reads_the_choice_and_ignores_junk():
    from kei_agent import router

    allowed = {"list-due", "ask"}
    assert router.parse('{"skill": "list-due", "days": 7}', allowed) == router.Choice(skill="list-due", params={"days": 7})
    # 前後に文が付いていても拾う
    assert router.parse('はい\n{"skill": "ask"}\n', allowed).skill == "ask"
    # 知らない仕事、読めない返事、日数が変なものは ask に回す
    assert router.parse('{"skill": "drop-database"}', allowed).skill == "ask"
    assert router.parse("よく分かりません", allowed).skill == "ask"
    assert router.parse('{"skill": "list-due", "days": 9999}', allowed).params == {}


def test_router_catalog_comes_from_the_card():
    from kei_agent import router

    text = router.catalog([{"id": "list-due", "description": "締切が近い順に JSON で返す\n2行目は捨てる"},
                           {"name": "名前だけ"}, {"id": "ask", "name": "授業のことに答える"}])
    assert text == "- list-due: 締切が近い順に JSON で返す\n- ask: 授業のことに答える"


async def test_course_channel_uses_the_router_choice(env, monkeypatch):
    """名刺のスキルを軽いモデルに選ばせ、その仕事を頼む（言葉の当ては使わない）。"""
    import json as _json

    from kei_agent import a2a, router

    assistant, slack, _, _ = env
    slack.channels["C7"] = "20_course"
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8787"

        async def card(self):
            return {"skills": [{"id": "list-due", "description": "締切"}, {"id": "ask", "description": "質問"}]}

        async def ask(self, skill, text="", params=None):
            asked.append((skill, params))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps({
                "ok": True, "text": "締切 0 件", "data": {"items": []},
                "limit_reset_at": None, "cost_usd": None}))

    assistant.agents["course"] = _Agent()

    async def fake_pick(config, skills, text):
        assert [s["id"] for s in skills] == ["list-due", "ask"]
        return router.Choice(skill="list-due", params={"days": 3})

    monkeypatch.setattr(router, "pick", fake_pick)

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "14.1",
                                "text": "<@UBOT> 来週までにやることある？"})
    await settle(assistant)

    assert asked == [("list-due", {"days": 3})]
    assert router.STATUS_TEXT in slack.thinking()


# 仕事のチャンネル（#30_work）


async def test_work_channel_asks_the_work_agent(env, monkeypatch):
    """仕事の依頼は仕事エージェントに取り次ぎ、予定を日ごとに分けて出す。"""
    import json as _json

    from kei_agent import a2a, router

    assistant, slack, claude, _ = env
    slack.channels["C8"] = "30_work"
    asked = []

    class _Agent:
        base_url = "http://127.0.0.1:8789"

        async def card(self):
            return {"skills": [{"id": "list-events", "description": "予定"}]}

        async def ask(self, skill, text="", params=None):
            asked.append((skill, params))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps({
                "ok": True, "text": "予定 1 件", "limit_reset_at": None, "cost_usd": None,
                "data": {"days": 1, "items": [{"subject": "朝会", "start": "2026-09-21T10:00:00",
                                               "end": "2026-09-21T10:15:00", "location": "Zoom"}]}}))

    assistant.agents["work"] = _Agent()

    async def fake_pick(config, skills, text):
        return router.Choice(skill="list-events", params={"days": 1})

    monkeypatch.setattr(router, "pick", fake_pick)

    await assistant.on_mention({"channel": "C8", "user": "UME", "ts": "15.1", "text": "<@UBOT> 今日の予定は？"})
    await settle(assistant)

    assert asked == [("list-events", {"days": 1})]
    assert claude.calls == []
    assert any("朝会" in (t or "") for t in slack.texts())


async def test_overview_channel_routes_to_the_right_agent(env, monkeypatch):
    """研究全体のチャンネルでは、どのエージェントの用事かも含めて判定し、そちらに回す。"""
    import json as _json

    from kei_agent import a2a, router

    assistant, slack, claude, _ = env
    asked = []

    class _Course:
        base_url = "http://127.0.0.1:8787"

        async def card(self):
            return {"skills": [{"id": "list-due", "description": "締切"}]}

        async def ask(self, skill, text="", params=None):
            asked.append((skill, params))
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps({
                "ok": True, "text": "締切 0 件", "data": {"items": []},
                "limit_reset_at": None, "cost_usd": None}))

    assistant.agents["course"] = _Course()

    async def fake_pick_across(config, by_agent, text):
        assert set(by_agent) == {"course"}
        return router.Choice(agent="course", skill="list-due", params={"days": 7})

    monkeypatch.setattr(router, "pick_across", fake_pick_across)

    await assistant.on_mention({"channel": "C5", "user": "UME", "ts": "16.1",
                                "text": "<@UBOT> 今週の課題の締切は？"})
    await settle(assistant)

    assert asked == [("list-due", {"days": 7})]
    assert claude.calls == []            # 研究の claude は動かさない


async def test_overview_agent_requests_use_the_same_thread_lock(env, monkeypatch):
    """研究全体から振り分けた依頼も、同じスレッドの中では1本ずつ動かす。"""
    import json

    from kei_agent import a2a, router

    assistant, _, _, _ = env
    gate = asyncio.Event()
    calls = 0

    class _Course:
        base_url = "http://127.0.0.1:8787"

        async def ask(self, skill, text="", params=None):
            nonlocal calls
            calls += 1
            await gate.wait()
            return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps({
                "ok": True, "text": "締切 0 件", "data": {"items": []},
                "limit_reset_at": None, "cost_usd": None}))

    assistant.agents["course"] = _Course()
    assistant.agent_skills["course"] = [{"id": "list-due", "description": "締切"}]
    assistant.agent_skills_read_at["course"] = time.time()

    async def fake_pick_across(config, by_agent, text):
        return router.Choice(agent="course", skill="list-due", params={"days": 7})

    async def fake_pick(config, skills, text):
        return router.Choice(skill="list-due", params={"days": 7})

    monkeypatch.setattr(router, "pick_across", fake_pick_across)
    # 2回目は「前と同じ相手（大学）」に回り、仕事の中身だけを選び直す
    monkeypatch.setattr(router, "pick", fake_pick)
    first = Request("C5", "research-overview", "20.1", None, "今週の締切")
    second = Request("C5", "research-overview", "20.1", None, "ほかには？")
    tasks = [asyncio.create_task(assistant.process(first)), asyncio.create_task(assistant.process(second))]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == 1
    gate.set()
    await asyncio.gather(*tasks)
    assert calls == 2


async def test_overview_channel_keeps_research_talk_in_house(env, monkeypatch):
    """研究の相談は、いままでどおり本体の claude が答える。"""
    from kei_agent import router

    assistant, slack, claude, _ = env

    class _Course:
        base_url = "http://127.0.0.1:8787"

        async def card(self):
            return {"skills": [{"id": "list-due", "description": "締切"}]}

    assistant.agents["course"] = _Course()

    async def fake_pick_across(config, by_agent, text):
        return router.Choice()          # どのエージェントでもない

    monkeypatch.setattr(router, "pick_across", fake_pick_across)

    await assistant.on_mention({"channel": "C5", "user": "UME", "ts": "16.2",
                                "text": "<@UBOT> 次の実験の方針を相談したい"})
    await settle(assistant)

    assert len(claude.calls) == 1        # 研究の claude が答える


def test_router_can_choose_between_agents():
    from kei_agent import router

    allowed = {"course:list-due", "work:list-events", "self"}
    assert router.parse('{"skill": "work:list-events"}', allowed) == router.Choice(
        agent="work", skill="list-events")
    assert router.parse('{"skill": "self"}', allowed).agent == ""
    # 知らない相手は、本体が自分で答える側に倒す
    assert router.parse('{"skill": "hr:fire-everyone"}', allowed).agent == ""


# 声で知らせる（voice.py）

class FakeVoiceAgent:
    base_url = "http://127.0.0.1:8790"

    def __init__(self):
        self.got = []

    async def ask(self, skill, text="", params=None):
        import json as _json

        from kei_agent import a2a
        self.got.append((skill, _json.loads(text)))
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=_json.dumps(
            {"ok": True, "text": "受け取ったよ", "data": {}, "limit_reset_at": None, "cost_usd": None}))


async def test_nothing_is_spoken_until_it_is_turned_on(env, store):
    """既定は切。机にロボットが無い状態で、急に喋り出さない。"""
    from kei_agent import settings

    assistant, slack, claude, _ = env
    agent = FakeVoiceAgent()
    assistant.agents["voice"] = agent

    assert settings.voice_enabled(store) is False
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> やって"})
    await settle(assistant)

    assert agent.got == []


async def test_events_are_sent_as_what_happened_not_as_words(env, store):
    """渡すのは出来事だけ。言い方も顔も、声のレイヤが決める。"""
    from kei_agent import settings

    assistant, slack, claude, _ = env
    agent = FakeVoiceAgent()
    assistant.agents["voice"] = agent
    settings.set_voice(store, True)

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> やって"})
    await settle(assistant)

    kinds = [e["kind"] for _, e in agent.got]
    assert "working" in kinds and "done" in kinds
    assert all(skill == "notify" for skill, _ in agent.got)
    # 文は入れない（本体が「どう言うか」を持つと、対応表が2か所に散る）
    assert all("text" not in e or e["kind"] == "schedule" for _, e in agent.got)
    assert {"kind": "done", "theme": "vlm"} in [e for _, e in agent.got]


async def test_a_dead_voice_layer_does_not_break_slack(env, store):
    """声が出なくても、Slack の仕事は終わっている（知らせるだけのことなので、依頼者に見せない）。"""
    from kei_agent import a2a, settings

    assistant, slack, claude, _ = env
    settings.set_voice(store, True)

    class Dead:
        base_url = "http://127.0.0.1:8790"

        async def ask(self, skill, text="", params=None):
            raise a2a.A2AError("名刺を読めません（HTTP 502）")

    assistant.agents["voice"] = Dead()

    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> やって"})
    await settle(assistant)

    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "white_check_mark"}) in slack.calls
    assert not any("頼めなかった" in (t or "") for t in slack.texts())
