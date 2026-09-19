import asyncio
from pathlib import Path

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack, write_request

from ezra import runner
from ezra.assistant import Assistant, history_prompt, split_text
from ezra.jobs import JobManager


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C9": "research-ezra", "C5": "research-overview"})
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
    # 道具を使うたびに、作業の手順を1行ずつ見せる
    assert [(c["title"], c["status"]) for c in slack.tasks()] == [("Bash: テスト", "in_progress"), ("Bash: テスト", "complete")]
    start, = [kw for name, kw in slack.calls if name == "chat_startStream"]
    assert start["task_display_mode"] == "timeline"
    assert "結果です" not in slack.texts()  # 流して見せたので、もう一度投稿しない
    assert store.get_thread("C1", "10.1")["session_id"] == "sess-1"
    log = (config.research_root / "vlm" / ".ezra" / "threads" / "10.1.md").read_text()
    assert "## 依頼者" in log and "図を作って" in log and "## Ezra" in log and "結果です" in log


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
    # Ezra が動いていないスレッド
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
    assert "依頼者: 条件Aで回して" in second["prompt"] and "Ezra: 条件Aの結果は 82% でした" in second["prompt"]
    assert "作業中" not in second["prompt"] and second["prompt"].count("Bも") == 1
    assert store.get_thread("C1", "10.1")["session_id"] == "sess-new"
    assert not any(t.startswith("⚠️") for t in slack.texts())


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
    assert slack.texts()[-1] == "⚠️ エラーで止まりました: rate limited"
    # 止まったときは ✅ ではなく ⚠️ をつける
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "warning"}) in slack.calls
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "white_check_mark"}) not in slack.calls


async def test_narration_goes_to_the_steps_not_the_answer(env):
    """途中の独り言は手順の補足にし、本文は最後のまとめだけにする。同じ話が2回並ばない。"""
    assistant, slack, claude, _ = env
    claude.behaviors = [{"steps": [("text", "了解、進めるね。CLAUDE.md に書いておく。"), ("tool", "Edit: CLAUDE.md")],
                         "text": "了解したよ。CLAUDE.md に書いておいた。"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> x"})
    await settle(assistant)

    assert slack.streamed() == ["了解したよ。CLAUDE.md に書いておいた。"]
    assert slack.tasks()[0]["details"] == "了解、進めるね。CLAUDE.md に書いておく。"


async def test_run_uses_domains_allowed_for_the_theme(env, store, monkeypatch):
    assistant, slack, claude, _ = env
    from ezra import settings
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

    backlog = (config.research_root / "_overview" / "backlog.md").read_text()
    assert "#research-ezra" in backlog and "経過をもっと細かく" in backlog
    assert "https://example.slack.com/archives/C9/p201" in backlog
    assert claude.calls == []
    assert slack.texts()[-1] == f"要望を `{config.research_root / '_overview' / 'backlog.md'}` に記録しました。"


async def test_thread_broadcast_reply_continues_thread(env, store):
    assistant, slack, claude, _ = env
    store.upsert_thread("C1", "10.1", "vlm", "s")
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
    assert texts[-1].startswith("⚠️ `outputs/` のファイルを添付できませんでした")
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
    assert claude.calls[0]["cwd"] == config.research_root / "_overview"


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
    assert "🧪 ジョブ 1「sweep」を投入しました: `scripts/sweep.py --n 50`" in slack.texts()
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
    assert "🧪 ジョブ 1「sweep」が終わりました（成功）。結果を確認します" in slack.texts()
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
    from ezra import themes

    def boom(ws):
        raise RuntimeError("ディスクが一杯です")

    monkeypatch.setattr(themes, "ensure_workspace", boom)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)

    texts = slack.texts()
    assert any("依頼の処理が落ちました" in t and "ディスクが一杯です" in t for t in texts)
    assert claude.calls == []


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
    from ezra import themes

    ws = themes.resolve(config, "research-overview")
    themes.ensure_workspace(ws)
    result = runner.RunResult(session_id=None, text="", is_error=True, errors=["起動できません"])

    thread_ts = await assistant.publish("C5", "research-overview", ws, "🌙 振り返りの材料", result)

    assert store.get_thread("C5", thread_ts) is not None


def test_message_text_reads_messages_posted_as_markdown():
    """流して見せた返事や markdown_text の投稿は、text が空で blocks に入る。"""
    from ezra.assistant import message_text

    assert message_text({"text": "ふつうの投稿"}) == "ふつうの投稿"
    assert message_text({"text": "", "blocks": [{"type": "markdown", "text": "流した返事"}]}) == "流した返事"
    assert message_text({"blocks": [{"type": "rich_text", "elements": [
        {"elements": [{"type": "text", "text": "書き込み"}]}]}]}) == "書き込み"


def test_theme_runs_remembers_overlap():
    """outputs/ はテーマで共通なので、重なって動いたことを覚えておく。"""
    from ezra.assistant import ThemeRuns

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

    assert any("別のスレッドの図が混ざっているかもしれません" in t for t in slack.texts())


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
    from ezra import settings
    claude.behaviors = [
        {"text": "Zenodo につながらなかった。\n🔒 接続: zenodo.org（CASTELLA の特徴量を落とすため）"},
        {"text": "落とせたよ"},
    ]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    allow, post = _button_action(slack, "ezra_domain_allow")
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
    from ezra import settings
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）"}, {"text": "別の入手先を探すね"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    deny, _ = _button_action(slack, "ezra_domain_deny")
    await assistant.on_domain_action(_press(deny))
    await settle(assistant)

    assert settings.theme_domains(store, "vlm") == []
    assert "断" in claude.calls[1]["prompt"]


async def test_only_the_allowed_user_can_press(env, store):
    assistant, slack, claude, _ = env
    from ezra import settings
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    allow, _ = _button_action(slack, "ezra_domain_allow")
    await assistant.on_domain_action(_press(allow, user="USOMEONE"))
    await settle(assistant)
    assert settings.theme_domains(store, "vlm") == [] and len(claude.calls) == 1


async def test_several_requests_resume_once_after_all_answered(env, store):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）\n🔒 接続: huggingface.co（重み）"}, {"text": "続けたよ"}]
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 落として"})
    await settle(assistant)

    buttons = [el for _, kw in slack.calls for b in kw.get("blocks") or [] for el in b.get("elements", [])
               if el.get("action_id") == "ezra_domain_allow"]
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
