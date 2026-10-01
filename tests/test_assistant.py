import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack, pending_asks, write_request

import kei_agent.conversation.assistant as assistant_module
from kei_agent.configuration import settings
from kei_agent.conversation import a2a, ask, router
from kei_agent.conversation.assistant import Assistant
from kei_agent.conversation.auto_messages import history_prompt
from kei_agent.conversation.request import Request
from kei_agent.conversation.slack_text import split_text
from kei_agent.conversation.thread_ui import ThreadUI
from kei_agent.execution import runner
from kei_agent.execution.execution_contract import prompt_version
from kei_agent.execution.jobs import JobManager
from kei_agent.testing.kit import settle
from kei_agent.workspaces import themes


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C9": "0-kei-agent", "C5": "research-overview"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    pueue = FakePueue()
    assistant = Assistant(config, store, slack, JobManager(config, store, pueue), "xoxb-test", "UBOT")
    return assistant, slack, claude, pueue




async def mention(assistant, text, ts="10.1", channel="C1", **extra):
    """メンションで頼み、動き終わるまで待つ。"""
    await assistant.on_mention({"channel": channel, "user": "UME", "ts": ts, "text": f"<@UBOT> {text}", **extra})
    await settle(assistant)


async def reply(assistant, text, ts, thread_ts="10.1", channel="C1", **extra):
    """スレッドにメンションなしで返信し、動き終わるまで待つ。"""
    await assistant.on_message({"channel": channel, "user": "UME", "ts": ts, "thread_ts": thread_ts,
                                "text": text, **extra})
    await settle(assistant)


def reacted(slack, name, ts="10.1", channel="C1", op="reactions_add"):
    return (op, {"channel": channel, "timestamp": ts, "name": name}) in slack.calls


def _mentions(slack):
    return [t for t in slack.texts() if t.startswith("<@UME>")]


def _known_thread(assistant, store, session):
    """前の回の会話（session）を覚えているスレッド（C1 の 10.1）にする。"""
    version = prompt_version(assistant.config, "research")
    store.upsert_thread("C1", "10.1", "vlm", session)
    store.set_session("C1", "10.1", "research", "claude", session, version)
    store.set_prompt_version("C1", "10.1", version)


def _hold_runs(monkeypatch, claude):
    """gate が開くまで AI の実行を止めておく。"""
    gate = asyncio.Event()

    async def slow(config, request, prompt, on_activity=None):
        await gate.wait()
        return await claude(config, request, prompt, on_activity)

    monkeypatch.setattr(runner, "run_model", slow)
    return gate


def _done(text, data=None):
    """定型の仕事の返事（本文が JSON）。"""
    return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps({
        "ok": True, "text": text, "data": data if data is not None else {"items": []},
        "limit_reset_at": None, "cost_usd": None}))


def _ask_result(data):
    """`ask` の返事。data に実行結果（本文・会話の番号・上限）が入る。"""
    ok = not data.get("is_error")
    return a2a.TaskResult(state="TASK_STATE_COMPLETED" if ok else "TASK_STATE_FAILED", text="",
                          status_text=json.dumps({"ok": ok, "text": data.get("text", ""), "data": data,
                                                  "limit_reset_at": data.get("limit_reset_at"), "cost_usd": None}))


def _final(text):
    return f"<<kei-agent-final>>\n{text}\n<<kei-agent-final-end>>"


ANSWER = _final("答えだよ")


class _Agent:
    """担当の偽物。頼まれた (skill, 本文の JSON, params) を asked に残し、reply を返す。

    reply は返事そのものか、skill を受けて返事を返す関数（例外を投げても、await が要るものでもよい）。
    skills を渡したときだけ名刺（card）を持つ。
    """

    def __init__(self, reply, skills=None, base_url="http://127.0.0.1:8787", activity=None):
        self.base_url = base_url
        self.reply = reply
        self.activity = activity
        self.asked: list[tuple[str, dict, dict]] = []
        if skills is not None:
            async def card():
                return {"skills": skills}
            self.card = card

    async def stream(self, skill, text="", params=None, on_progress=None):
        self.asked.append((skill, json.loads(text) if text.startswith("{") else {}, dict(params or {})))
        if on_progress and self.activity:
            await on_progress(json.dumps({"activity": self.activity}))
        result = self.reply(skill) if callable(self.reply) else self.reply
        if asyncio.iscoroutine(result):
            result = await result
        return result


class _AskAgent(_Agent):
    """`ask` だけを受ける担当の偽物。replies の data を順に返す。asked には頼んだ本文の JSON を残す。"""

    def __init__(self, base_url: str, replies: list[dict]):
        super().__init__(None, base_url=base_url)
        self.replies = list(replies)

    async def stream(self, skill, text="", params=None, on_progress=None):
        assert skill == "ask"
        self.asked.append(json.loads(text))
        return _ask_result(self.replies.pop(0))


async def test_mention_runs_claude_in_theme_and_replies(env, config, store):
    assistant, slack, claude, _ = env

    await mention(assistant, "図を作って")

    call, = claude.calls
    assert call["cwd"] == config.research_root / "vlm"
    assert call["prompt"] == "図を作って" and call["session_id"] is None and call["thread_ts"] == "10.1"
    assert (config.research_root / "vlm" / "CLAUDE.md").exists()
    assert reacted(slack, "eyes")
    # 作業中は agent session を processing にし、終わったら active に戻す。返事は流しながら見せる
    assert slack.statuses() == ["processing", "active"]
    # 終わったら 👀 を外して ✅ にする。どの依頼に答えたかが一目で分かる
    assert reacted(slack, "eyes", op="reactions_remove") and reacted(slack, "white_check_mark")
    assert slack.streamed() == ["結果です"]
    assert any(name == "chat_stopStream" for name, _ in slack.calls)
    # 経過は入力欄の下の1行だけに出し、返事には手順を並べない（長い作業でもスクロールが要らない）
    assert slack.tasks() == []
    assert slack.thinking() == ["考え中…", "作業している…"]
    assert "結果です" not in slack.texts()  # 流して見せたので、もう一度投稿しない
    assert store.get_thread("C1", "10.1")["session_id"] == "sess-1"
    log = (config.research_root / "vlm" / ".kei-agent" / "threads" / "10.1.md").read_text()
    assert "## 依頼者" in log and "図を作って" in log and "## Kei Agent" in log and "結果です" in log


@pytest.mark.parametrize(("behavior", "shown", "hidden"), [
    ({"text": "まず材料を確認します。\n" + _final("僕が調べた結果、できたよ。")}, "僕が調べた結果、できたよ。",
     "まず材料を確認します。"),
    ({"text": "材料を確認してから返します", "raw": True}, "返答を利用者向けの形に整えられなかったよ",
     "材料を確認してから返します"),
])
async def test_only_the_final_part_of_the_model_text_is_posted(env, behavior, shown, hidden):
    """AI の前置きや、決まった形になっていない返事を、そのまま Slack に出さない。"""
    assistant, slack, claude, _ = env
    claude.behaviors = [behavior]

    await mention(assistant, "質問")

    posted = "\n".join(slack.streamed() + slack.texts())
    assert shown in posted and hidden not in posted


async def test_thread_status_never_uses_raw_model_text():
    slack = FakeSlack({"C1": "vlm"})
    await ThreadUI(slack, "C1", "10.1").activity("Read: /private/secret")
    assert slack.thinking() == ["調べている…"]


async def test_messages_that_should_not_trigger(env):
    assistant, slack, claude, _ = env
    # ほかの人のメンションには、何も反応しない
    await assistant.on_mention({"channel": "C1", "user": "USOMEONE", "ts": "10.1", "text": "<@UBOT> hi"})
    await settle(assistant)
    assert slack.calls == []
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


async def test_switching_provider_uses_slack_history_not_the_old_session(env, store):
    """AI を切り替えたら（行きも戻りも）、前の AI の会話は再開せず、Slack の履歴から文脈を戻す。"""
    assistant, slack, claude, _ = env
    await mention(assistant, "始めて")
    settings.set_agent_provider(store, "research", "codex")
    slack.replies = [
        {"ts": "10.1", "user": "UME", "text": "始めて"},
        {"ts": "10.2", "user": "UBOT", "bot_id": "B1", "text": "前回の答え"},
        {"ts": "10.3", "user": "UME", "text": "続きを"},
    ]
    await reply(assistant, "続きを", "10.3")

    assert claude.calls[1]["session_id"] is None
    assert "前回の答え" in claude.calls[1]["prompt"]
    assert store.last_provider("C1", "10.1", "research") == "codex"

    settings.set_agent_provider(store, "research", "claude")
    slack.replies.append({"ts": "10.4", "user": "UBOT", "bot_id": "B1", "text": "二回目"})
    await reply(assistant, "さらに", "10.5")
    assert claude.calls[2]["session_id"] is None
    assert "二回目" in claude.calls[2]["prompt"]


async def test_missing_session_is_restored_from_thread_history(env, store):
    assistant, slack, claude, _ = env
    _known_thread(assistant, store, "lost-session")
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

    await reply(assistant, "Bも", "10.5")

    first, second = claude.calls
    assert first["session_id"] == "lost-session" and second["session_id"] is None
    assert "依頼者: 条件Aで回して" in second["prompt"] and "Kei Agent: 条件Aの結果は 82% でした" in second["prompt"]
    assert "作業中" not in second["prompt"] and second["prompt"].count("Bも") == 1
    assert store.get_thread("C1", "10.1")["session_id"] == "lost-session"
    assert store.session_for("C1", "10.1", "research", "claude",
                             prompt_version(assistant.config, "research")) == "sess-new"
    assert not any(t.startswith("⚠️") for t in slack.texts())


async def test_long_thread_history_is_paged_and_says_what_it_dropped(env, store, monkeypatch):
    """履歴が長いスレッドでは、新しいほうから読み、落とした分があることをプロンプトに書く。"""
    assistant, slack, claude, _ = env
    monkeypatch.setattr(assistant_module, "HISTORY_PAGE", 2)
    monkeypatch.setattr(assistant_module, "HISTORY_MAX_MESSAGES", 4)
    _known_thread(assistant, store, "lost-session")
    texts = ["いちばん古い話", "その次", "三つめ", "四つめ", "五つめ", "いちばん新しい話"]
    pages = [([{"ts": f"10.{i + j + 1}", "user": "UME", "text": texts[i + j]} for j in range(2)], cursor)
             for i, cursor in zip((0, 2, 4), ("c1", "c2", ""), strict=True)]
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

    await reply(assistant, "続き", "10.7")

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


def _makes_figure(cwd):
    (cwd / "outputs").mkdir(exist_ok=True)
    (cwd / "outputs" / "fig.png").write_bytes(b"png")


@pytest.mark.parametrize("overlapped", [False, True])
async def test_new_outputs_are_uploaded(env, config, overlapped):
    """新しくできたものだけを添付する。同じテーマで別のスレッドが動いていたら、図が混ざりうることを黙って隠さない。"""
    assistant, slack, claude, _ = env
    old = config.research_root / "vlm" / "outputs"
    old.mkdir(parents=True)
    (old / "old.png").write_bytes(b"old")
    claude.behaviors = [{"side_effect": _makes_figure}]
    if overlapped:
        assistant.theme_runs.begin("vlm", "99.1")

    await mention(assistant, "図")

    upload, = [kw for name, kw in slack.calls if name == "files_upload_v2"]
    assert [f["filename"] for f in upload["file_uploads"]] == ["fig.png"]
    assert upload["thread_ts"] == "10.1"
    assert any("別のスレッドの図が混ざっているかもしれない" in t for t in slack.texts()) is overlapped


async def test_upload_failure_does_not_hide_result(env, monkeypatch):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"side_effect": _makes_figure}]
    monkeypatch.setattr(slack, "files_upload_v2", AsyncMock(side_effect=RuntimeError("upload failed")))

    await mention(assistant, "図")

    assert slack.streamed() == ["結果です"]
    texts = slack.texts()
    assert texts[-1] == "⚠️ 結果のファイルを添付できなかったよ。もう一度頼んでね。"
    assert not any("内部エラー" in t for t in texts)


@pytest.mark.parametrize("long_run", [False, True])
async def test_error_result_is_reported(env, store, monkeypatch, long_run):
    """止まったら ⚠️ を付けて言う。返事待ちにはしない。長くかかった回だけメンションで知らせる。"""
    assistant, slack, claude, _ = env
    if long_run:
        monkeypatch.setattr(assistant_module, "NOTIFY_AFTER_SECONDS", -1)
    claude.behaviors = [{"is_error": True, "text": "", "errors": ["rate limited"]}]

    await mention(assistant, "x")

    assert "⚠️ 接続に失敗したよ。少し時間を置いてもう一度頼んでね。" in slack.streamed()
    assert reacted(slack, "warning") and not reacted(slack, "white_check_mark")
    # 24時間後の声かけも「返事がほしいよ」も出さない。返信すれば続きとしてやる
    assert store.threads_awaiting() == []
    assert _mentions(slack) == (["<@UME> 止まったよ"] if long_run else [])
    assert store.get_thread("C1", "10.1")["stalled_request"] == "x"


@pytest.mark.parametrize("steps", [[("text", "了解、進めるね。CLAUDE.md に書いておく。"), ("tool", "Edit: CLAUDE.md")], []])
async def test_narration_goes_to_the_status_not_the_answer(env, steps):
    """途中の独り言は本文に出さず、本文は最後のまとめにする（道具を使わない回も流して見せる）。"""
    assistant, slack, claude, _ = env
    final = "了解したよ。CLAUDE.md に書いておいた。"
    claude.behaviors = [{"steps": steps, "text": final}]

    await mention(assistant, "x")

    assert slack.streamed() == [final] and slack.tasks() == [] and final not in slack.texts()
    assert "了解、進めるね" not in "\n".join(slack.thinking() + slack.texts() + slack.streamed())


async def test_run_uses_domains_allowed_for_the_theme(env, store, monkeypatch):
    assistant, slack, claude, _ = env
    settings.allow_domain(store, "vlm", "zenodo.org", "")
    seen = []

    async def spy(config, request, *args, **kwargs):
        seen.append(request.workspace.allowed_domains)
        return await claude(config, request, *args, **kwargs)

    monkeypatch.setattr(runner, "run_model", spy)
    await mention(assistant, "x")
    assert seen == [("zenodo.org",)]


async def test_question_suspends_the_session_and_mentions_the_owner(env):
    """依頼者の判断を待つときは、Slack 側でも「返事待ち」に見せ、メンションで知らせる。"""
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "どちらにしますか\n❓ 確認: A と B のどちらにしますか"}]
    await mention(assistant, "x")
    assert slack.statuses() == ["processing", "suspended"]
    assert len(_mentions(slack)) == 1


async def test_improve_channel_files_the_request_as_a_public_issue(env, config, fake_github):
    assistant, slack, claude, _ = env

    await mention(assistant, "経過をもっと細かく", ts="20.1", channel="C9")

    # 要約だけを公開の issue にしたうえで、直し方の案を考える（自己改善のモジュール。書けるのは作業用のフォルダだけ）
    assert len(fake_github.created()) == 1
    assert claude.calls[0]["cwd"] == config.module_state("improve") / "talk" / "20.1"
    assert "GitHub issue <https://github.com/97kuek/kei-agent/issues/1|#1>" in "\n".join(slack.texts())
    assert not (config.overview_dir / "backlog.md").exists()


async def test_thread_broadcast_reply_continues_thread(env, store):
    assistant, slack, claude, _ = env
    _known_thread(assistant, store, "s")
    await reply(assistant, "チャンネルにも送った返信", "10.2", subtype="thread_broadcast")
    assert claude.calls[0]["prompt"] == "チャンネルにも送った返信"


async def test_channel_rename_forgets_cached_name(env):
    assistant, slack, claude, _ = env
    assert await assistant.channel_name("C1") == "vlm"
    slack.channels["C1"] = "vlm-counting"
    await assistant.on_channel_rename({"channel": {"id": "C1", "name": "vlm-counting"}})
    assert await assistant.channel_name("C1") == "vlm-counting"


async def test_same_thread_requests_run_one_at_a_time(env, monkeypatch):
    """同じスレッドの依頼は1つずつ動かす。

    1件目が終わった直後に3件目が来ても、2件目と同時には走らせない。
    同じスレッド = 同じセッションなので、2本の claude が同時に動くと会話が混ざる。
    """
    assistant, slack, claude, _ = env
    gates = [asyncio.Event() for _ in range(3)]
    state = {"running": 0, "peak": 0, "calls": 0}

    async def gated(config, request, prompt, on_activity=None):
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

    monkeypatch.setattr(runner, "run_model", gated)
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
    await mention(assistant, "x")
    first = assistant.thread_locks[("C1", "10.1")]
    await mention(assistant, "y", ts="10.2", thread_ts="10.1")
    assert assistant.thread_locks[("C1", "10.1")] is first


async def test_overview_channel_runs_in_overview_dir(env, config):
    assistant, slack, claude, _ = env
    await mention(assistant, "全体を見て", ts="30.1", channel="C5")
    assert claude.calls[0]["cwd"] == config.overview_dir


async def test_job_submitted_during_run_then_resumed_when_finished(env, config, store):
    assistant, slack, claude, pueue = env

    def submit_job(cwd: Path):
        (cwd / "scripts").mkdir(exist_ok=True)
        (cwd / "scripts" / "sweep.py").write_text("print(1)")
        write_request(cwd, action="submit", request_id="req-1", channel="C1", thread_ts="10.1",
                      name="sweep", script="scripts/sweep.py", args=["--n", "50"])

    claude.behaviors = [{"side_effect": submit_job, "text": "ジョブを投入しました"}, {"text": "集計しました"}]
    await mention(assistant, "回して")

    assert len(pueue.added) == 1
    assert "🧪 ジョブ 1「sweep」を投入したよ: `scripts/sweep.py --n 50`" in slack.texts()
    # ジョブが走っている間は、スレッドを作業中のままにしておく
    assert slack.statuses()[-1] == "processing"

    await assistant.poll_jobs()  # まだ終わっていない
    await settle(assistant)
    assert len(claude.calls) == 1

    (config.research_root / "vlm" / "outputs" / "result.csv").write_text("a,b\n")
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
    assert "終わった" in _mentions(slack)[-1]  # ジョブの報告は、短くてもメンションで知らせる
    upload, = [kw for name, kw in slack.calls if name == "files_upload_v2"]
    assert [f["filename"] for f in upload["file_uploads"]] == ["result.csv"]

    await assistant.poll_jobs()  # 二度は報告しない
    await settle(assistant)
    assert len(claude.calls) == 2


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


async def test_job_loop_notifies_once_while_failing(env, monkeypatch):
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
    notices = [t for t in slack.texts() if "確認が必要な問題" in t]
    assert len(notices) == 1 and slack.posted()[-1]["channel"] == "C9"


async def test_crash_before_the_run_is_reported_to_the_thread(env, monkeypatch):
    """作業用ディレクトリを作れないときなどに、👀 がついたまま黙って終わらない。"""
    assistant, slack, claude, _ = env

    def boom(ws):
        raise RuntimeError("ディスクが一杯です")

    monkeypatch.setattr(themes, "ensure_workspace", boom)

    await mention(assistant, "図を作って")

    texts = slack.texts()
    assert "⚠️ 接続に失敗したよ。少し時間を置いてもう一度頼んでね。" in texts
    assert not any("ディスクが一杯です" in t for t in texts)
    assert claude.calls == [] and reacted(slack, "warning")


def _unavailable(error):
    from slack_sdk.errors import SlackApiError

    async def call(**kw):
        raise SlackApiError(error, {"ok": False})

    return call


@pytest.mark.parametrize(("methods", "error", "streamed"), [
    # 文言を指定する API が断られても、状態の切り替えと返事は今までどおり
    (("assistant_threads_setStatus",), "missing_scope", True),
    # ステータスと流し見せが使えないワークスペースでは、今までどおり投稿する
    (("agents_sessions_setStatus", "chat_startStream"), "method_not_supported_for_channel_type", False),
])
async def test_keeps_working_when_new_slack_apis_are_unavailable(env, monkeypatch, methods, error, streamed):
    assistant, slack, claude, _ = env
    for name in methods:
        monkeypatch.setattr(slack, name, _unavailable(error), raising=False)

    await mention(assistant, "図を作って")

    if streamed:
        assert slack.statuses() == ["processing", "active"] and slack.streamed() == ["結果です"]
    else:
        assert slack.posted()[-1] == {"channel": "C1", "thread_ts": "10.1", "markdown_text": "結果です"}
        assert slack.streamed() == []


def test_message_text_reads_messages_posted_as_markdown():
    """流して見せた返事や markdown_text の投稿は、text が空で blocks に入る。"""
    from kei_agent.conversation.slack_text import message_text

    assert message_text({"text": "ふつうの投稿"}) == "ふつうの投稿"
    assert message_text({"text": "", "blocks": [{"type": "markdown", "text": "流した返事"}]}) == "流した返事"
    assert message_text({"blocks": [{"type": "rich_text", "elements": [
        {"elements": [{"type": "text", "text": "書き込み"}]}]}]}) == "書き込み"


def test_theme_runs_remembers_overlap():
    """outputs/ はテーマで共通なので、重なって動いたことを覚えておく。"""
    from kei_agent.conversation.assistant import ThemeRuns

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


# 接続先の申し出（🔒 接続:）

def _buttons(slack, name):
    """投稿されたボタンのうち、action_id が name のもの（新しい順）と、その投稿。"""
    return [(el, kw) for _, kw in reversed(slack.calls) for block in kw.get("blocks") or []
            for el in block.get("elements", []) if el.get("action_id") == name]


def _press(el, user="UME", message_ts="99.1"):
    return {"user": {"id": user}, "actions": [{"action_id": el["action_id"], "value": el["value"]}],
            "container": {"channel_id": "C1", "message_ts": message_ts}, "channel": {"id": "C1"}}


async def test_connect_request_becomes_buttons_and_resumes_when_allowed(env, store):
    assistant, slack, claude, _ = env
    claude.behaviors = [
        {"text": "Zenodo につながらなかった。\n🔒 接続: zenodo.org（CASTELLA の特徴量を落とすため）"},
        {"text": "落とせたよ"},
    ]
    await mention(assistant, "落として")

    (allow, post), = _buttons(slack, "kei_agent_domain_allow")
    assert post["thread_ts"] == "10.1" and "zenodo.org" in post["text"]
    assert slack.statuses()[-1] == "suspended"  # 返事待ちに見せる
    assert len(_mentions(slack)) == 1

    await assistant.on_domain_action(_press(allow))
    await settle(assistant)

    assert settings.theme_domains(store, "vlm") == ["zenodo.org"]
    update = [kw for name, kw in slack.calls if name == "chat_update"][-1]
    assert "許可しました" in update["text"] and not any(b.get("type") == "actions" for b in update["blocks"])
    resumed = claude.calls[1]
    assert resumed["session_id"] == "sess-1" and "zenodo.org" in resumed["prompt"] and "許可" in resumed["prompt"]


async def test_connect_request_is_answered_only_by_the_owner_and_denial_resumes(env, store):
    """ほかの人が押しても何も変わらない。断ったら、そのことを伝えて続きをやらせる。"""
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）"}, {"text": "別の入手先を探すね"}]
    await mention(assistant, "落として")

    (allow, _), = _buttons(slack, "kei_agent_domain_allow")
    await assistant.on_domain_action(_press(allow, user="USOMEONE"))
    await settle(assistant)
    assert settings.theme_domains(store, "vlm") == [] and len(claude.calls) == 1

    (deny, _), = _buttons(slack, "kei_agent_domain_deny")
    await assistant.on_domain_action(_press(deny))
    await settle(assistant)
    assert settings.theme_domains(store, "vlm") == []
    assert "断" in claude.calls[1]["prompt"]


async def test_several_requests_resume_once_after_all_answered(env):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "🔒 接続: zenodo.org（特徴量）\n🔒 接続: huggingface.co（重み）"}, {"text": "続けたよ"}]
    await mention(assistant, "落として")

    second, first = [el for el, _ in _buttons(slack, "kei_agent_domain_allow")]
    await assistant.on_domain_action(_press(first))
    await settle(assistant)
    assert len(claude.calls) == 1  # もう1つに答えるまで待つ

    await assistant.on_domain_action(_press(second))
    await assistant.on_domain_action(_press(second))  # 2度押し
    await settle(assistant)
    assert len(claude.calls) == 2
    assert "zenodo.org" in claude.calls[1]["prompt"] and "huggingface.co" in claude.calls[1]["prompt"]


async def test_connect_request_for_an_allowed_domain_asks_nothing(env):
    assistant, slack, claude, _ = env
    claude.behaviors = [{"text": "🔒 接続: export.arxiv.org（論文）"}]  # config.toml で許可済み
    await mention(assistant, "探して")
    assert not any(kw.get("blocks") for _, kw in slack.calls if "blocks" in kw and kw.get("text", "").startswith("🔒"))


async def test_domains_asked_through_the_bash_tool_also_become_buttons(env, monkeypatch):
    """Claude が 🔒 の行を書かず、Bash の allowed_domains で頼んだときも、ボタンを出す。"""
    assistant, slack, claude, _ = env

    async def asks_with_tool(config, request, *args, **kwargs):
        result = await claude(config, request, *args, **kwargs)
        result.requested_domains = [("huggingface.co", "重みを落とす")]
        return result

    monkeypatch.setattr(runner, "run_model", asks_with_tool)
    await mention(assistant, "落として")
    (_, post), = _buttons(slack, "kei_agent_domain_allow")
    assert "huggingface.co" in post["text"] and "重みを落とす" in post["text"]


async def test_updated_rules_start_a_fresh_session_once(env, monkeypatch):
    """メンションなしの返信は前の会話の続き。指示版が変わったら旧 session を再開せず、Slack 履歴で新規 session を作る。"""
    assistant, slack, claude, _ = env
    version = {"v": "v1"}
    monkeypatch.setattr(assistant_module, "prompt_version", lambda config, actor, workspace=None: version["v"])

    await mention(assistant, "始めて")
    await reply(assistant, "続き", "10.2")
    assert claude.calls[1]["session_id"] == "sess-1" and claude.calls[1]["prompt"] == "続き"
    version["v"] = "v2"
    await reply(assistant, "もう一度", "10.3")
    await reply(assistant, "さらに", "10.4")
    assert claude.calls[2]["session_id"] is None
    assert "Slack のスレッドの履歴" in claude.calls[2]["prompt"]
    assert claude.calls[2]["prompt"].endswith("もう一度")
    assert claude.calls[3]["session_id"] == "sess-1"
    assert claude.calls[3]["prompt"] == "さらに"


# 契約の上限（Claude AI usage limit）

def _limit(reset=None):
    return {"is_error": True, "text": "Claude AI usage limit reached" + (f"|{int(reset)}" if reset else ""), "errors": []}


async def test_usage_limit_is_retried_after_it_resets(env, store):
    """上限に達したら、明ける時刻を伝えて、そのあと自動でやり直す。"""
    assistant, slack, claude, _ = env
    reset = time.time() + 3600
    claude.behaviors = [_limit(reset), {"text": "やり直したよ"}]

    await mention(assistant, "図を作って")

    texts = "\n".join(slack.texts())
    assert "上限" in texts and "やり直す" in texts
    assert "エラーで止まっちゃった" not in texts        # ふつうのエラーとしては出さない
    assert store.limit_until("claude") > time.time()
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
    claude.behaviors = [_limit()]
    await mention(assistant, "x")
    assert time.time() + 60 < store.limit_until("claude") <= time.time() + assistant.LIMIT_FALLBACK_SECONDS + 5


async def test_quota_retry_does_not_auto_switch_provider(env, store):
    assistant, slack, claude, _ = env
    reset = time.time() + 3600
    claude.behaviors = [_limit(reset)]
    await mention(assistant, "調べて")
    settings.set_agent_provider(store, "research", "codex")

    await assistant.retry_deferred(now=reset + 120)
    await settle(assistant)

    assert len(claude.calls) == 1
    assert not store.pending_deferred("request")
    assert any("自動で再実行しなかった" in text for text in slack.texts())


async def test_next_request_after_an_error_carries_the_stalled_request(env, store):
    assistant, slack, claude, _ = env
    slack.replies = [
        {"ts": "10.1", "user": "UME", "text": "<@UBOT> 図を作って"},
        {"ts": "10.5", "user": "UME", "text": "続けて"},
    ]
    claude.behaviors = [{"is_error": True, "text": "", "errors": ["boom"], "session_id": "s1"},
                        {"session_id": "s2"}, {"session_id": "s3"}]
    await mention(assistant, "図を作って")
    await reply(assistant, "続けて", "10.5")

    second = claude.calls[1]
    assert second["session_id"] is None
    assert "止まった依頼" in second["prompt"] and "図を作って" in second["prompt"]
    assert second["prompt"].endswith("続きの依頼:\n続けて")
    assert store.get_thread("C1", "10.1")["stalled_request"] is None    # 成功したので忘れる

    await reply(assistant, "次", "10.6")
    assert claude.calls[2]["session_id"] == "s2" and claude.calls[2]["prompt"] == "次"


# Slack の外からの依頼（声のレイヤ。docs/architecture.md）

async def _handle_asks(assistant):
    await assistant.handle_asks()
    await settle(assistant)


async def test_ask_from_outside_starts_a_thread_and_runs(env, config):
    assistant, slack, claude, _ = env
    ask.write_ask(config, "vlm", "条件ごとの精度を集計して")

    await _handle_asks(assistant)

    posted = [kw for name, kw in slack.calls if name == "chat_postMessage"]
    assert "🎤 声からの依頼" in posted[0]["text"] and "条件ごとの精度" in posted[0]["text"]
    assert claude.calls[0]["prompt"] == "条件ごとの精度を集計して"
    assert claude.calls[0]["cwd"] == config.research_root / "vlm"
    assert pending_asks(config) == []      # 拾ったら消す


async def test_ask_from_outside_is_kept_until_it_is_taken_and_never_posted_twice(env, config, monkeypatch):
    """Slack へのスレッド作成や受付が一時失敗しても、依頼を次回へ残す。再試行しても Slack スレッドは重複させない。"""
    assistant, slack, _, _ = env
    path = ask.write_ask(config, "vlm", "集計して")
    with monkeypatch.context() as broken:
        broken.setattr(slack, "chat_postMessage", AsyncMock(side_effect=RuntimeError("temporary")))
        with pytest.raises(RuntimeError, match="temporary"):
            await assistant.handle_asks()
    assert path.exists() and pending_asks(config)[0][1]["text"] == "集計して"

    submit = AsyncMock(side_effect=[RuntimeError("temporary"), None])
    monkeypatch.setattr(assistant, "submit", submit)
    with pytest.raises(RuntimeError, match="temporary"):
        await assistant.handle_asks()

    assert path.exists()
    persisted = pending_asks(config)[0][1]
    assert persisted["text"] == "集計して" and persisted["thread_ts"] == "1001.000"

    await assistant.handle_asks()

    assert len(slack.posted()) == 1
    assert [call.args[0].thread_ts for call in submit.await_args_list] == [persisted["thread_ts"]] * 2
    assert pending_asks(config) == []


async def test_ask_loop_recovers_interrupted_asks_only_before_polling(env, config, monkeypatch):
    """再起動時の回収は1回だけ行い、通常の poll では所有権を保つ。"""
    assistant, _, _, _ = env
    events = []

    monkeypatch.setattr(ask, "recover_asks", lambda actual: events.append(("recover", actual)))

    async def handle_asks():
        events.append(("handle", config))
        if len(events) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(assistant, "handle_asks", handle_asks)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    with pytest.raises(asyncio.CancelledError):
        await assistant.ask_loop()

    assert events == [("recover", config), ("handle", config), ("handle", config)]


async def test_note_from_outside_is_only_recorded_once_even_when_completion_is_retried(env, config, monkeypatch):
    """決まったことは、作業させずにスレッドに残すだけ。投稿後の完了に失敗しても、同じスレッドを使い回す。"""
    assistant, slack, claude, _ = env
    path = ask.write_ask(config, "vlm", "4条件の比較で進める", kind="note")
    real_complete = ask.complete_ask
    attempts = []

    def complete_once_retried(item):
        attempts.append(item)
        if len(attempts) == 1:
            raise RuntimeError("temporary")
        real_complete(item)

    monkeypatch.setattr(ask, "complete_ask", complete_once_retried)

    with pytest.raises(RuntimeError, match="temporary"):
        await assistant.handle_asks()

    assert path.exists()
    assert pending_asks(config)[0][1]["thread_ts"] == "1001.000"

    await _handle_asks(assistant)

    notes = [text for text in slack.texts() if text.startswith("📌 声で決まったこと")]
    assert len(notes) == 1
    assert claude.calls == []
    assert pending_asks(config) == []


async def test_ask_for_an_unknown_theme_is_reported(env, config):
    assistant, slack, claude, _ = env
    ask.write_ask(config, "nothere", "何かして")

    await _handle_asks(assistant)

    assert claude.calls == []
    assert "確認が必要な問題" in "\n".join(slack.texts())
    assert pending_asks(config) == []


@pytest.mark.parametrize(("long_run", "text", "mentioned"), [
    (False, "こんにちは", False),      # 短い回はメンションしない
    (True, "集計して", True),          # 長くかかった回は「終わったよ」と知らせる
    (True, "今どんな感じ", False),     # 様子を答えただけの回は、何かが終わったわけではない
])
async def test_finished_run_mentions_only_after_a_long_job(env, monkeypatch, long_run, text, mentioned):
    assistant, slack, claude, _ = env
    if long_run:
        monkeypatch.setattr(assistant_module, "NOTIFY_AFTER_SECONDS", -1)
    await mention(assistant, text)
    assert ["終わった" in m for m in _mentions(slack)] == ([True] if mentioned else [])


# 再起動で途中で止まった依頼のやり直し


async def test_request_is_recorded_while_it_runs(env, store, monkeypatch):
    """強制終了されても拾えるよう、claude が動いている間は控えが残っている。終わったら消え、やり直すものは無い。"""
    assistant, _, claude, _ = env
    seen = []

    async def watching(*args, **kw):
        seen.append([p["text"] for _, p in store.interrupted_requests()])
        return await claude(*args, **kw)

    monkeypatch.setattr(runner, "run_model", watching)
    await mention(assistant, "図を作って")

    assert seen == [["図を作って"]]
    assert store.interrupted_requests() == []
    assert await assistant.resume_interrupted() == 0


async def test_cancelled_request_stays_recorded_for_the_next_start(env, store, monkeypatch):
    """終了処理でキャンセルされた依頼は、次の起動でやり直せるよう控えを残す。"""
    assistant, _, claude, _ = env
    _hold_runs(monkeypatch, claude)
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
    """強制終了で控えが残ったまま起動したときは、断って続きからやり直す。開いたままの実行の記録は閉じる。"""
    assistant, slack, claude, _ = env
    run_id = store.start_run("C1", "10.1", "vlm", "message")
    store.start_in_flight(Request("C1", "vlm", "10.1", "10.1", "図を作って").to_payload())

    assert await assistant.resume_interrupted() == 1
    await settle(assistant)

    assert store.interrupted_requests() == []
    assert "途中で止まっちゃった" in slack.texts()[0]
    assert "図を作って" in claude.calls[0]["prompt"] and "再起動で途中で止まりました" in claude.calls[0]["prompt"]
    assert reacted(slack, "white_check_mark")
    row = store.conn.execute("SELECT ended_at, is_error FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["ended_at"] is not None and row["is_error"] == 1


async def test_busy_thread_answers_status_at_once_and_queues_the_next_request(env, monkeypatch):
    """処理中に様子を聞かれたらキューに積まずその場で答え、次の依頼には黙って待たせず一言返して順番に処理する。"""
    assistant, slack, claude, _ = env
    gate = _hold_runs(monkeypatch, claude)
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 集計して"})
    await asyncio.sleep(0)
    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1",
                                "text": "今どんな感じ？"})
    await asyncio.sleep(0)

    assert any("処理してる" in t for t in slack.texts())
    assert reacted(slack, "eyes", ts="10.2", op="reactions_remove") and reacted(slack, "white_check_mark", ts="10.2")

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.3", "thread_ts": "10.1", "text": "図も"})
    await asyncio.sleep(0)
    assert "いま前の作業をしているから" in slack.texts()[-1]

    gate.set()
    await settle(assistant)
    # 様子を尋ねる一言は claude に渡らない
    assert [c["prompt"] for c in claude.calls] == ["集計して", "図も"]
    assert sum("いま前の作業" in (t or "") for t in slack.texts()) == 1


async def limited_thread(assistant, slack, claude, store):
    """上限で止まって、明けてからやり直す予定のスレッド（C1 の 10.1）にする。"""
    store.upsert_thread("C1", "10.1", "vlm", "sess-1")
    claude.behaviors = [{"is_error": True, "text": "You've hit your session limit · resets 6:30pm (Asia/Tokyo)"}]
    await mention(assistant, "集計して")
    assert any("上限に達したみたい" in (t or "") for t in slack.texts())
    assert len(store.pending_deferred("request")) == 1
    assert store.get_thread("C1", "10.1")["stalled_request"] == "集計して"


async def test_writing_during_the_limit_waits_until_it_ends(env, store):
    """上限の間に続きを書いても、いまは動かさない（同じ上限に当たって知らせが重なる）。⏳ を付け、明けたらこの続きとしてやる。"""
    assistant, slack, claude, _ = env
    await limited_thread(assistant, slack, claude, store)

    # 様子を聞かれたら、いつ続きをやるかを答える（予約はそのまま）
    await reply(assistant, "進捗は？", "10.2")
    assert "利用上限で止まっているよ" in slack.texts()[-1] and "ごろに続きからやる" in slack.texts()[-1]
    (_, payload), = store.pending_deferred("request")
    assert payload["text"] == "集計して"
    posts = len(slack.texts())

    await reply(assistant, "続けて", "10.3")

    assert len(claude.calls) == 1 and len(slack.texts()) == posts          # 動かさず、投稿もしない
    assert reacted(slack, "hourglass_flowing_sand", ts="10.3")
    (_, payload), = store.pending_deferred("request")
    assert payload["text"] == "続けて" and payload["held"] == ["10.3"]

    claude.behaviors = [{"text": "集計したよ"}]
    await assistant.retry_deferred(now=2 ** 31)
    await settle(assistant)

    # 止まった依頼を文脈として渡す（「続けて」だけでは何を続けるか分からない）
    assert len(claude.calls) == 2 and "集計して" in claude.calls[1]["prompt"]
    assert reacted(slack, "hourglass_flowing_sand", ts="10.3", op="reactions_remove")
    assert "終わった" in _mentions(slack)[-1]                              # あとでやり直した回は、短くても知らせる
    assert store.pending_deferred("request") == []


async def test_writing_after_switching_the_provider_runs_now(env, store):
    """別の provider に切り替えていれば、上限を待たずに、止まった依頼の続きとしてすぐ動かす。"""
    assistant, slack, claude, _ = env
    await limited_thread(assistant, slack, claude, store)
    settings.set_agent_provider(store, "research", "codex")

    await reply(assistant, "続けて", "10.2")

    assert store.pending_deferred("request") == []
    assert any("自動のやり直しをやめて" in (t or "") for t in slack.texts())
    assert "集計して" in claude.calls[1]["prompt"]
    await assistant.retry_deferred(now=2 ** 31)
    await settle(assistant)
    assert len(claude.calls) == 2


async def test_attachments_are_passed_again_when_the_request_is_retried(env, store, monkeypatch):
    assistant, slack, claude, _ = env

    async def download(files, cwd, token):
        (cwd / "inputs").mkdir(exist_ok=True)
        for f in files:
            (cwd / "inputs" / f["name"]).write_text("a,b\n")
        return [f"inputs/{f['name']}" for f in files]

    monkeypatch.setattr(assistant_module, "download_files", download)
    reset = time.time() + 3600
    claude.behaviors = [_limit(reset), {"text": "見たよ"}]
    await mention(assistant, "これ見て", files=[{"name": "data.csv", "url_private": "https://files.example/data.csv"}])

    (_, payload), = store.pending_deferred("request")
    assert payload["saved_files"] == ["inputs/data.csv"]
    await assistant.retry_deferred(now=reset + 120)
    await settle(assistant)
    assert "添付ファイル（保存先）:\n- inputs/data.csv" in claude.calls[1]["prompt"]


@pytest.mark.parametrize("long_run", [False, True])
async def test_limit_notice_mentions_the_owner_only_after_a_long_run(env, store, monkeypatch, long_run):
    """上限の回は返事待ちではない。App Home には、上限が明けるのを待っていると出す。"""
    from kei_agent.conversation import home

    assistant, slack, claude, _ = env
    if long_run:
        monkeypatch.setattr(assistant_module, "NOTIFY_AFTER_SECONDS", -1)
    claude.behaviors = [_limit(time.time() + 3600)]
    await mention(assistant, "図を作って")
    notice, = [t for t in slack.texts() if "上限に達したみたい" in t]
    assert notice.startswith("<@UME> ⚠️" if long_run else "⚠️")
    assert store.threads_awaiting() == []
    line, = home._now_working(assistant.config, store)
    assert line.startswith("⏸ *#vlm* 上限が明けるのを待っている（") and "ごろから" in line


async def test_status_question_in_an_idle_thread_runs_read_only(env):
    """何も動いていないスレッドで様子を聞かれただけなら、読むだけで動かす（作業は始まらない）。"""
    assistant, slack, claude, _ = env
    await mention(assistant, "図を作って")
    await reply(assistant, "どうなってる？", "10.2")
    await reply(assistant, "進捗表示を直して", "10.3")
    assert [call["read_only"] for call in claude.calls] == [False, True, False]


# 大学のチャンネル（#2-course）・仕事のチャンネル（#3-work）


@pytest.fixture
def course(env):
    """#2-course（C7）を足した env。"""
    env[1].channels["C7"] = "2-course"
    return env


async def test_course_channel_asks_the_university_agent(course):
    """大学の依頼は claude を動かさず、大学エージェントに取り次いで返事をそのまま出す。"""
    assistant, slack, claude, _ = course
    agent = assistant.agents["course"] = _Agent(a2a.TaskResult(state="TASK_STATE_COMPLETED", text="新しい課題 2 件"))

    await mention(assistant, "課題を取り込んで", ts="11.1", channel="C7")

    assert agent.asked == [("sync-assignments", {}, {"provider": "claude"})]
    assert "新しい課題 2 件" in slack.texts()
    assert claude.calls == []
    assert reacted(slack, "white_check_mark", ts="11.1", channel="C7")


@pytest.mark.parametrize(("actor", "channel", "name", "skill", "state", "shown"), [
    ("course", "C7", "2-course", "sync-assignments", "TASK_STATE_FAILED", "接続に失敗したよ"),
    # completed でも、定型処理に混ざった例外は出さない
    ("course", "C7", "2-course", "sync-assignments", "TASK_STATE_COMPLETED", "返答を利用者向けの形に整えられなかったよ"),
    ("work", "C8", "3-work", "list-events", "TASK_STATE_FAILED", "接続に失敗したよ"),
])
async def test_routine_a2a_hides_failure_details(env, actor, channel, name, skill, state, shown):
    """定型 A2A の例外やローカルパスは Slack に中継しない。"""
    assistant, slack, _, _ = env
    slack.channels[channel] = name
    assistant.agents[actor] = _Agent(a2a.TaskResult(state=state, text="RuntimeError: /private/secret"))

    await assistant.modules[actor].on_message(Request(channel, name, "11.1", "11.1", "取り込んで"),
                                              skill=skill, params={})

    posted = "\n".join(slack.texts())
    assert shown in posted
    assert "RuntimeError" not in posted and "/private/secret" not in posted


async def test_course_channel_sends_free_questions_to_ask(course, store):
    """定型に当てはまらない質問は ask に回し、大学エージェント自身の claude が答える。"""
    assistant, slack, claude, _ = course
    agent = assistant.agents["course"] = _Agent(_ask_result({
        "session_id": "course-1", "text": _final("過去問は Box の Personal/過去問 にあるよ"), "is_error": False,
        "provider": "claude"}), activity="box_search: 過去問")

    await mention(assistant, "情報セキュリティBの過去問ある？", ts="11.2", channel="C7")

    (skill, payload, _), = agent.asked
    assert skill == "ask" and payload["prompt"] == "情報セキュリティBの過去問ある？"
    assert payload["session_id"] is None and payload["thread_ts"] == "11.2"
    assert claude.calls == []                      # 本体では claude を動かさない
    assert "調べている…" in slack.thinking()  # 経過は固定の1行だけに出す
    assert slack.streamed() == ["過去問は Box の Personal/過去問 にあるよ"]
    # 会話の続きは本体が覚える（次の質問は同じ provider の会話につながる。研究と同じ表）
    provider = settings.selected_provider(assistant.config, store, "course")
    assert store.session_for("C7", "11.2", "course", provider,
                             prompt_version(assistant.config, "course")) == "course-1"


@pytest.mark.parametrize(("long_run", "answer", "said"), [
    (False, "過去問は Box にあるよ", None),
    (True, "過去問は Box にあるよ", "終わった"),
    (False, "どの科目の過去問？\n❓ 確認: 情報セキュリティAとBのどちら？", "返事がほしい"),
])
async def test_course_answers_mention_the_owner_like_research(course, monkeypatch, long_run, answer, said):
    """大学・仕事・知識のスレッドでも、長くかかった回と返事がほしい回は、研究と同じくメンションで知らせる。"""
    assistant, slack, _, _ = course
    if long_run:
        monkeypatch.setattr(assistant_module, "NOTIFY_AFTER_SECONDS", -1)
    assistant.agents["course"] = _AskAgent("http://127.0.0.1:8787", [
        {"session_id": "course-1", "text": _final(answer), "is_error": False, "provider": "claude"}])
    await mention(assistant, "過去問ある？", ts="11.2", channel="C7")
    assert [said in m for m in _mentions(slack)] == ([True] if said else [])


async def test_course_error_is_not_left_waiting_for_a_reply(course, store):
    """大学の担当が止まった（ログイン切れなど）スレッドを、返事待ちにしない（24時間後に声をかけない）。"""
    assistant, slack, _, _ = course
    assistant.agents["course"] = _Agent(a2a.TaskResult(state="TASK_STATE_COMPLETED", text="", status_text=json.dumps({
        "ok": True, "text": "", "limit_reset_at": None, "cost_usd": None,
        "data": {"session_id": None, "text": "", "is_error": True, "provider": "claude",
                 "errors": ["Not logged in"]}})))
    await mention(assistant, "過去問ある？", ts="11.2", channel="C7")

    assert reacted(slack, "warning", ts="11.2", channel="C7")
    assert store.threads_awaiting() == [] and _mentions(slack) == []


@pytest.mark.parametrize(("channel", "name", "actor"), [("C7", "2-course", "course"), ("C8", "3-work", "work")])
async def test_course_and_work_threads_continue_the_agents_session_like_research(env, store, channel, name, actor):
    """大学・仕事の続きは、Slack の履歴を貼り直さず前回の会話を再開する。provider が変わったら履歴から文脈を戻す。"""
    assistant, slack, _, _ = env
    slack.channels[channel] = name
    agent = assistant.agents[actor] = _AskAgent("http://127.0.0.1:8787", [{"text": ANSWER, "session_id": f"{actor}-1"}] * 3)

    await mention(assistant, "どうなってる？", ts="11.2", channel=channel)
    await reply(assistant, "その続きは？", "11.3", thread_ts="11.2", channel=channel)

    first, second = agent.asked
    assert first["session_id"] is None and second["session_id"] == f"{actor}-1"
    assert second["prompt"] == "その続きは？" and "Slack のスレッドの履歴" not in second["prompt"]
    assert (second["channel"], second["thread_ts"], second["read_only"]) == (channel, "11.2", False)
    assert slack.streamed() == ["答えだよ", "答えだよ"]

    settings.set_agent_provider(store, actor, "codex")
    slack.replies = [
        {"ts": "11.2", "user": "UME", "text": "どうなってる？"},
        {"ts": "11.3", "user": "UBOT", "bot_id": "B1", "text": "前の答え"},
        {"ts": "11.4", "user": "UME", "text": "それは？"},
    ]
    await reply(assistant, "それは？", "11.4", thread_ts="11.2", channel=channel)

    third = agent.asked[2]
    assert third["session_id"] is None and "前の答え" in third["prompt"] and third["provider"] == "codex"
    assert store.last_provider(channel, "11.2", actor) == "codex"


async def test_course_limit_is_deferred_and_retried_like_research(course, store):
    """大学・仕事でも、上限に当たったら明ける時刻をスレッドに書き、明けてから自動でやり直す。"""
    assistant, slack, _, _ = course
    assistant.agents["course"] = _AskAgent("http://127.0.0.1:8787", [
        {"is_error": True, "limit_reset_at": time.time() + 3600, "provider": "claude"}])

    await mention(assistant, "どうなってる？", ts="11.2", channel="C7")

    assert [payload["thread_ts"] for _, payload in store.pending_deferred("request")] == ["11.2"]
    assert any("利用上限" in text and "自動でやり直す" in text for text in slack.texts())


async def test_every_agent_request_starts_with_todays_date(course, monkeypatch):
    """「今日の授業は？」に答えられるよう、どの担当への依頼にも今日の日付と曜日を先頭に付ける。"""
    from kei_agent.conversation import auto_messages

    monkeypatch.setattr(assistant_module, "today_line", auto_messages.today_line)
    assistant, slack, claude, _ = course
    agent = assistant.agents["course"] = _AskAgent("http://127.0.0.1:8787", [{"text": ANSWER, "session_id": "c-1"}])

    await assistant.on_mention({"channel": "C7", "user": "UME", "ts": "11.2", "text": "<@UBOT> 今日の授業は？"})
    await mention(assistant, "図を作って")

    today = auto_messages.today_line()
    assert today.startswith("今日は ") and today.endswith("。\n")
    assert agent.asked[0]["prompt"] == today + "今日の授業は？"
    assert claude.calls[0]["prompt"] == today + "図を作って"


async def test_improve_without_a_provider_says_to_choose_one(env, store):
    """自己改善の AI が選ばれていないときは「接続に失敗」ではなく、選ぶよう伝える。"""
    from dataclasses import replace

    from kei_agent.configuration.config import AgentProfile

    assistant, slack, claude, _ = env
    assistant.config = replace(assistant.config, agent_profiles={
        **assistant.config.agent_profiles, "improve": AgentProfile(provider="")})
    assert settings.selected_provider(assistant.config, store, "improve") == ""

    result = await assistant.run_agent(themes.resolve(assistant.config, "0-kei-agent"), "直して")

    assert result.is_error and assistant.render_reply(result)[0].startswith("⚠️ 使う AI")
    assert claude.calls == []


async def test_overview_thread_keeps_asking_the_agent_that_answered(env, monkeypatch):
    """研究全体のスレッドの続きは、会話の鍵を持たない相手（仕事）でも、同じ相手のまま続ける。"""
    assistant, _, _, _ = env
    picked = []
    assistant.agents.clear()
    work = assistant.agents["work"] = _Agent(_done("予定はないよ"), base_url="http://127.0.0.1:8789")
    assistant.agent_skills["work"] = [{"id": "list-events", "description": "予定"}]
    assistant.agent_skills_read_at["work"] = time.time()

    async def fake_pick_across(config, by_agent, text, *, store=None):
        picked.append(text)
        return router.Choice(agent="work", skill="list-events")

    async def fake_pick(config, skills, text, *, store=None):
        return router.Choice(skill="list-events")

    monkeypatch.setattr(router, "pick_across", fake_pick_across)
    monkeypatch.setattr(router, "pick", fake_pick)

    await assistant.process(Request("C5", "research-overview", "21.1", None, "今日の会議は？"))
    await assistant.process(Request("C5", "research-overview", "21.1", None, "そのあとは？"))

    assert [skill for skill, _, _ in work.asked] == ["list-events", "list-events"]
    assert picked == ["今日の会議は？"]  # 2回目は、どのエージェントに聞くかを選び直さない


async def test_course_channel_formats_the_deadlines(course, store):
    """締切は JSON で返ってくるので、Slack 向けの短い行に組み直して出す。続きはメンションなしの返信でも頼める。"""
    assistant, slack, _, _ = course
    agent = assistant.agents["course"] = _Agent(_done("締切 1 件", {
        "days": 14, "more": 2, "items": [{"id": "1@moodle", "at": "2026-09-21T17:00:00+09:00",
                                          "course": "プロジェクト研究B", "title": "履修申請フォーム"}]}))

    await mention(assistant, "締切を教えて", ts="12.1", channel="C7")

    assert agent.asked == [("list-due", {}, {"provider": "claude"})]
    shown = slack.texts()[-1]
    assert "9/21（月） 17:00 プロジェクト研究B / 履修申請フォーム" in shown
    assert "（ほかに 2 件）" in shown

    # スレッドを覚えていないと、メンションなしの返信を拾えない
    assert store.get_thread("C7", "12.1") is not None
    await reply(assistant, "来週の締切は？", "12.5", thread_ts="12.1", channel="C7")
    assert [skill for skill, _, _ in agent.asked] == ["list-due", "list-due"]


DUES = [{"id": "a@moodle", "at": "2026-10-25T23:59:00+09:00", "course": "データベース", "title": "Assignment A"},
        {"id": "b@moodle", "at": "2026-11-01T23:59:00+09:00", "course": "データベース", "title": "Assignment B"},
        {"id": "c@moodle", "at": "2026-11-21T23:59:00+09:00", "course": "統計解析実習", "title": "課題#1"},
        {"id": "d@moodle", "at": "2026-11-21T23:59:00+09:00", "course": "統計解析実習", "title": "課題#2"}]


def _due_agent(by_days):
    """締切の一覧（list-due）を返す大学の担当。days ごとに、決めておいた締切を返す。"""
    agent = _Agent(None, skills=[{"id": "list-due", "description": "締切"}, {"id": "ask", "description": "質問"}])

    def reply(skill):
        days = agent.asked[-1][1].get("days", 14)
        items = by_days.get(days, [])
        return _done(f"締切 {len(items)} 件", {"days": days, "more": 0, "items": items})

    agent.reply = reply
    return agent


def _pick(monkeypatch, **choice):
    async def fake_pick(config, skills, text, *, store=None):
        return router.Choice(**choice)

    monkeypatch.setattr(router, "pick", fake_pick)


async def test_course_channel_adds_the_nearest_deadline_when_none_is_near(course, monkeypatch):
    """2週間に締切が無くても「ない」だけで終わらせず、その先のいちばん近いものを添える（2026-09-26 21:40）。"""
    assistant, slack, _, _ = course
    agent = assistant.agents["course"] = _due_agent({14: [], 400: DUES})
    _pick(monkeypatch, skill="list-due")

    await mention(assistant, "最近締め切りの課題ってあったけ", ts="15.1", channel="C7")

    assert [body.get("days") for _, body, _ in agent.asked] == [None, 400]
    shown = slack.texts()[-1]
    assert shown.startswith("これから2週間の締切はないよ。いちばん近いのはこれ。")
    assert "Assignment A" in shown and "Assignment B" not in shown


@pytest.mark.parametrize(("choice", "text"), [
    ({"days": 365, "limit": 1}, "一番近い課題は？"),     # 振り分け係が件数を拾った
    ({"days": 365}, "一番締め切りが近い課題は？全期間で"),  # 拾えなくても、言い方で1件と分かる
])
async def test_course_channel_answers_the_nearest_deadline_alone(course, monkeypatch, choice, text):
    """「一番近い」に全部を並べない（2026-09-26 21:41、1年ぶん30件を並べていた）。"""
    assistant, slack, _, _ = course
    assistant.agents["course"] = _due_agent({365: DUES})
    _pick(monkeypatch, skill="list-due", params=dict(choice))

    await mention(assistant, text, ts="16.1", channel="C7")

    shown = slack.texts()[-1]
    assert "Assignment A" in shown and "Assignment B" not in shown and "ほかに" not in shown


def test_first_items_keep_deadlines_at_the_same_time():
    from kei_agent_modules.course import module as course

    assert [item["id"] for item in course.first_items(DUES, 3)] == ["a@moodle", "b@moodle", "c@moodle", "d@moodle"]
    assert [item["id"] for item in course.first_items(DUES, 1)] == ["a@moodle"]


async def test_an_expired_login_says_so_and_tells_the_improve_channel_once(course):
    """ログインが切れた担当は「接続に失敗」ではなく、そう言う。改善のチャンネルには入り直し方を1回だけ（2026-09-27）。"""
    assistant, slack, _, _ = course
    how = runner.login_help("claude", {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-personal"})
    failed = {"is_error": True, "failure_kind": "login", "provider": "claude",
              "errors": [how, "Failed to authenticate: OAuth session expired and could not be refreshed"]}
    assistant.agents["course"] = _AskAgent("http://127.0.0.1:8787", [dict(failed), dict(failed)])

    for ts in ("17.1", "18.1"):
        await mention(assistant, "学校はいつから？", ts=ts, channel="C7")

    shown = slack.texts() + slack.streamed()
    replies = [text for text in shown if "ログインが切れていて" in text]
    assert len(replies) == 2 and not any("接続に失敗" in text for text in shown)
    notices = [text for text in slack.texts() if "大学の担当の AI が動きません" in text]
    assert len(notices) == 1 and ".claude-personal" in notices[0] and "claude auth login" in notices[0]


async def test_course_channel_tells_when_the_agent_is_down(course):
    """大学エージェントにつながらないときは、スレッドに言って `#0-kei-agent` にも知らせる。"""
    assistant, slack, _, _ = course

    def down(skill):
        raise a2a.A2AError("名刺を読めません（HTTP 502）")

    assistant.agents["course"] = _Agent(down)

    await mention(assistant, "締切を教えて", ts="12.2", channel="C7")

    assert any("接続に失敗したよ" in (t or "") for t in slack.texts())
    assert any("Kei Agent で確認が必要な問題が起きたよ" in (t or "") for t in slack.texts())
    assert reacted(slack, "warning", ts="12.2", channel="C7")


async def test_course_channel_waits_out_a_restart_instead_of_failing(course, monkeypatch):
    """入れ替えの最中で一瞬つながらないだけなら、待ってやり直して失敗を見せない。"""
    from kei_agent.conversation import agents

    assistant, slack, _, _ = course
    monkeypatch.setattr(agents, "RETRY_WAIT", 0)

    def restarting(skill):
        if len(agent.asked) == 1:
            raise a2a.NotReachable("つながりません（http://127.0.0.1:8787）")
        return a2a.TaskResult(state="completed", text=json.dumps({"ok": True, "text": "締切はないよ", "data": {"due": []}}))

    agent = assistant.agents["course"] = _Agent(restarting)

    await mention(assistant, "締切を教えて", ts="12.3", channel="C7")

    assert len(agent.asked) == 2
    assert not any("頼めなかった" in (t or "") for t in slack.texts())
    assert not reacted(slack, "warning", ts="12.3", channel="C7")


# 研究エージェント（A2A）に実行を任せる


async def test_claude_runs_through_the_research_agent_when_configured(env, store):
    """[a2a] research_url を書くと、claude は研究エージェント経由で動く（本体の中では動かさない）。"""
    assistant, slack, claude, _ = env
    agent = assistant.agents["research"] = _Agent(_ask_result(
        {"text": _final("できたよ"), "session_id": "sess-7", "is_error": False}),
        base_url="http://127.0.0.1:8788", activity="Bash: 図を描く")

    await mention(assistant, "図を作って", ts="13.1")

    assert claude.calls == []
    (skill, payload, _), = agent.asked
    assert skill == "ask"
    assert payload["channel_name"] == "vlm" and payload["prompt"] == "図を作って"
    assert payload["channel"] == "C1" and payload["thread_ts"] == "13.1"
    assert payload["provider"] == "claude"
    # 経過は入力欄の下の1行に出し、返事はいつもどおり流して見せる
    assert "作業している…" in slack.thinking()
    assert slack.streamed() == ["できたよ"]
    assert store.get_thread("C1", "13.1")["session_id"] == "sess-7"


# 振り分け係（軽いモデルで、どの仕事かを選ぶ）


def test_router_reads_the_choice_and_ignores_junk():
    allowed = {"list-due", "ask"}
    assert router.parse('{"skill": "list-due", "days": 7}', allowed) == router.Choice(skill="list-due", params={"days": 7})
    # 前後に文が付いていても拾う
    assert router.parse('はい\n{"skill": "ask"}\n', allowed).skill == "ask"
    # 知らない仕事、読めない返事、日数が変なものは ask に回す
    assert router.parse('{"skill": "drop-database"}', allowed).skill == "ask"
    assert router.parse("よく分かりません", allowed).skill == "ask"
    assert router.parse('{"skill": "list-due", "days": 9999}', allowed).params == {}
    # 件数（「一番近い」なら 1）も拾う。変な値は捨てる
    assert router.parse('{"skill": "list-due", "days": 365, "limit": 1}', allowed).params == {"days": 365, "limit": 1}
    assert router.parse('{"skill": "list-due", "limit": 0}', allowed).params == {}
    # 担当をまたいで選ぶときは「担当:仕事」。知らない相手は、本体が自分で答える側に倒す
    across = {"course:list-due", "work:list-events", "self"}
    assert router.parse('{"skill": "work:list-events"}', across) == router.Choice(agent="work", skill="list-events")
    assert router.parse('{"skill": "self"}', across).agent == ""
    assert router.parse('{"skill": "hr:fire-everyone"}', across).agent == ""


def test_router_catalog_comes_from_the_card():
    text = router.catalog([{"id": "list-due", "description": "締切が近い順に JSON で返す\n2行目は捨てる"},
                           {"name": "名前だけ"}, {"id": "ask", "name": "授業のことに答える"}])
    assert text == "- list-due: 締切が近い順に JSON で返す\n- ask: 授業のことに答える"


async def test_course_channel_uses_the_router_choice(course, monkeypatch):
    """名刺のスキルを軽いモデルに選ばせ、その仕事を頼む（言葉の当ては使わない）。"""
    assistant, slack, _, _ = course
    agent = assistant.agents["course"] = _Agent(
        _done("締切 0 件"), skills=[{"id": "list-due", "description": "締切"}, {"id": "ask", "description": "質問"}])

    async def fake_pick(config, skills, text, *, store=None):
        assert [s["id"] for s in skills] == ["list-due", "ask"]
        return router.Choice(skill="list-due", params={"days": 3})

    monkeypatch.setattr(router, "pick", fake_pick)

    await mention(assistant, "来週までにやることある？", ts="14.1", channel="C7")

    assert agent.asked == [("list-due", {"days": 3}, {"provider": "claude"})]
    assert "作業している…" in slack.thinking()


@pytest.mark.parametrize("question,expected,excluded,minimum_days", [
    ("今日の予定は？", "朝会", "定例", 1),
    ("明日の予定を教えて", "定例", "朝会", 2),
])
async def test_work_channel_asks_the_work_agent(env, monkeypatch, question, expected, excluded, minimum_days):
    """仕事の予定は依頼された日の分だけを取り次いで出す。"""
    assistant, slack, claude, _ = env
    slack.channels["C8"] = "3-work"
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    agent = assistant.agents["work"] = _Agent(_done("予定 1 件", {"days": minimum_days, "items": [
        {"subject": "朝会", "start": f"{today}T10:00:00", "end": f"{today}T10:15:00", "location": "Zoom"},
        {"subject": "定例", "start": f"{tomorrow}T14:00:00", "end": f"{tomorrow}T15:00:00", "location": "Teams"}]}),
        skills=[{"id": "list-events", "description": "予定"}], base_url="http://127.0.0.1:8789")
    _pick(monkeypatch, skill="list-events", params={"days": 1})

    await mention(assistant, question, ts="15.1", channel="C8")

    # 日数は本文の JSON で、provider は metadata で渡す（明日を聞かれたら、明日まで入るように2日）
    assert agent.asked == [("list-events", {"days": minimum_days}, {"provider": "claude"})]
    assert claude.calls == []
    shown = "\n".join(slack.texts())
    assert expected in shown and excluded not in shown


async def test_overview_channel_routes_to_the_right_agent(env, monkeypatch):
    """研究全体のチャンネルでは、どのエージェントの用事かも含めて判定し、そちらに回す。研究の相談は本体の claude が答える。"""
    assistant, slack, claude, _ = env
    agent = assistant.agents["course"] = _Agent(_done("締切 0 件"), skills=[{"id": "list-due", "description": "締切"}])
    choices = [router.Choice(agent="course", skill="list-due", params={"days": 7}), router.Choice()]

    async def fake_pick_across(config, by_agent, text, *, store=None):
        assert set(by_agent) == {"course"}
        return choices.pop(0)

    monkeypatch.setattr(router, "pick_across", fake_pick_across)

    await mention(assistant, "今週の課題の締切は？", ts="16.1", channel="C5")
    assert agent.asked == [("list-due", {"days": 7}, {"provider": "claude"})]
    assert claude.calls == []            # 研究の claude は動かさない

    await mention(assistant, "次の実験の方針を相談したい", ts="16.2", channel="C5")
    assert len(claude.calls) == 1 and len(agent.asked) == 1


async def test_overview_agent_requests_use_the_same_thread_lock(env, monkeypatch):
    """研究全体から振り分けた依頼も、同じスレッドの中では1本ずつ動かす。"""
    assistant, _, _, _ = env
    gate = asyncio.Event()

    async def held(skill):
        await gate.wait()
        return _done("締切 0 件")

    agent = assistant.agents["course"] = _Agent(held)
    assistant.agent_skills["course"] = [{"id": "list-due", "description": "締切"}]
    assistant.agent_skills_read_at["course"] = time.time()

    async def fake_pick_across(config, by_agent, text, *, store=None):
        return router.Choice(agent="course", skill="list-due", params={"days": 7})

    monkeypatch.setattr(router, "pick_across", fake_pick_across)
    # 2回目は「前と同じ相手（大学）」に回り、仕事の中身だけを選び直す
    _pick(monkeypatch, skill="list-due", params={"days": 7})
    first = Request("C5", "research-overview", "20.1", None, "今週の締切")
    second = Request("C5", "research-overview", "20.1", None, "ほかには？")
    tasks = [asyncio.create_task(assistant.process(first)), asyncio.create_task(assistant.process(second))]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(agent.asked) == 1
    gate.set()
    await asyncio.gather(*tasks)
    assert len(agent.asked) == 2


# 声で知らせる（声のモジュール modules/voice/。出来事を core.emit で配り、声のモジュールが担当に渡す）

class FakeVoiceAgent:
    base_url = "http://127.0.0.1:8790"

    def __init__(self):
        self.got = []

    async def ask(self, skill, text="", params=None):
        self.got.append((skill, json.loads(text)))
        return _done("受け取ったよ", {})


async def test_events_are_spoken_only_when_turned_on_and_as_what_happened(env):
    """既定は切（机にロボットが無いのに急に喋り出さない）。入れたら、渡すのは出来事だけ（言い方は声のレイヤが決める）。"""
    assistant, slack, claude, _ = env
    agent = assistant.agents["voice"] = FakeVoiceAgent()

    assert assistant.modules["voice"].is_on("notify") is False
    await mention(assistant, "やって")
    assert agent.got == []

    assistant.cores["voice"].records.put("switch", "notify", {"on": True})
    await mention(assistant, "やって", ts="10.2")

    kinds = [e["kind"] for _, e in agent.got]
    assert "working" in kinds and "done" in kinds
    assert all(skill == "notify" for skill, _ in agent.got)
    # 文は入れない（本体が「どう言うか」を持つと、対応表が2か所に散る）
    assert all("text" not in e or e["kind"] == "schedule" for _, e in agent.got)
    assert {"kind": "done", "theme": "vlm"} in [e for _, e in agent.got]


async def test_a_dead_voice_layer_does_not_break_slack(env):
    """声が出なくても、Slack の仕事は終わっている（知らせるだけのことなので、依頼者に見せない）。"""
    assistant, slack, claude, _ = env
    assistant.cores["voice"].records.put("switch", "notify", {"on": True})

    class Dead:
        base_url = "http://127.0.0.1:8790"

        async def ask(self, skill, text="", params=None):
            raise a2a.A2AError("名刺を読めません（HTTP 502）")

    assistant.agents["voice"] = Dead()

    await mention(assistant, "やって")

    assert reacted(slack, "white_check_mark")
    assert not any("頼めなかった" in (t or "") for t in slack.texts())


async def test_explicit_use_case_survives_the_handoff_memo(env, store, monkeypatch):
    """引き継いだスレッドの最初の回でも、先頭の [[research-design]] を読み取って分類器に回さない。"""
    from kei_agent.execution import model_classifier

    assistant, _, claude, _ = env

    async def classify(_config, _store, _spec, prompt, **_kw):
        raise AssertionError("明示指定があるのに分類器に回った")

    # 研究の分類器は、研究のモジュールの classify（classify_module）
    monkeypatch.setattr(model_classifier, "classify_module", classify)
    store.upsert_thread("C1", "10.1", "vlm", None)
    store.update_thread("C1", "10.1", handoff_memo="前のスレッドの要点: 実験Aは終わった\n\n")
    await reply(assistant, "[[research-design]] 次の実験を考えて", "10.2")

    prompt = claude.calls[-1]["prompt"]
    assert "[[research-design]]" not in prompt and "実験Aは終わった" in prompt and "次の実験を考えて" in prompt


async def test_unselected_provider_tells_where_to_choose(env, config):
    from dataclasses import replace as dc_replace

    assistant, slack, claude, _ = env
    profiles = dict(config.agent_profiles) | {"research": dc_replace(config.agent_profiles["research"], provider="")}
    object.__setattr__(assistant.config, "agent_profiles", profiles)
    await mention(assistant, "図を作って")

    shown = "\n".join(slack.streamed() + slack.texts())
    assert "agents.csv の engine 列に書いて" in shown and claude.calls == []


async def test_knowledge_channel_and_paper_threads_go_to_the_knowledge_agent(env, store):
    """#4-knowledge は知識の担当へ。テーマのチャンネルでも、朝の論文の新着のスレッドだけは知識の担当が答える。"""
    assistant, slack, claude, _ = env
    slack.channels["C40"] = "4-knowledge"
    agent = _AskAgent("http://127.0.0.1:8792", [{"text": ANSWER, "session_id": f"k-{i}"} for i in (1, 2, 3)])
    assistant.agents["knowledge"] = agent
    store.upsert_thread("C1", "20.1", "vlm", None)
    store.set_agent_session("C1", "20.1", "knowledge", "")

    await mention(assistant, "これ要約して https://zenn.dev/x", ts="40.1", channel="C40")
    slack.replies = [{"ts": "20.1", "user": "UBOT", "text": "📚 先行研究の新着\n\n1. *A*\n\n2. *Counting with VLMs*"},
                     {"ts": "20.2", "user": "UME", "text": "2番を詳しく"}]
    await reply(assistant, "2番を詳しく", "20.2", thread_ts="20.1")
    # 人が始めたスレッドには、今までどおり履歴を足さない
    slack.replies = [{"ts": "40.5", "user": "UME", "text": "自分のメモ"}, {"ts": "40.6", "user": "UME", "text": "x"}]
    await mention(assistant, "これどう思う？", ts="40.6", channel="C40", thread_ts="40.5")
    slack.replies = []
    await mention(assistant, "図を作って", ts="21.1")

    first, second, third = agent.asked
    assert first["channel"] == "C40" and first["prompt"].endswith("これ要約して https://zenn.dev/x")
    assert (second["channel"], second["thread_ts"]) == ("C1", "20.1") and second["prompt"].endswith("2番を詳しく")
    # 朝の投稿への最初の返信には、元の投稿（番号つきの一覧）を渡す
    assert "Kei Agent: 📚 先行研究の新着" in second["prompt"] and "Counting with VLMs" in second["prompt"]
    assert "自分のメモ" not in third["prompt"] and third["prompt"].endswith("これどう思う？")
    # ほかのスレッドは、今までどおり研究の担当
    assert [call["prompt"] for call in claude.calls][-1].endswith("図を作って")


async def test_a_deferred_question_in_a_paper_thread_follows_the_knowledge_provider(env, store):
    """論文の新着のスレッドの質問は知識の担当のもの。研究の担当のモデルを変えても、やり直しは止めない。"""
    assistant, slack, _, _ = env
    reset = time.time() + 3600
    agent = _AskAgent("http://127.0.0.1:8792", [{"is_error": True, "limit_reset_at": reset, "provider": "claude"},
                                                 {"text": ANSWER, "session_id": "k-1"}])
    assistant.agents["knowledge"] = agent
    store.upsert_thread("C1", "20.1", "vlm", None)
    store.set_agent_session("C1", "20.1", "knowledge", "")
    await reply(assistant, "2番を詳しく", "20.2", thread_ts="20.1")
    settings.set_agent_provider(store, "research", "codex")

    await assistant.retry_deferred(now=reset + 120)
    await settle(assistant)

    assert len(agent.asked) == 2 and agent.asked[1]["prompt"].endswith("2番を詳しく")
    assert not any("自動で再実行しなかった" in text for text in slack.texts())


async def test_troubles_in_a_row_become_one_message_that_says_notion_was_down(config, store):
    """Notion が止まると続けて失敗する。30分のうちの問題は最初の1通に書き足す（通知が鳴るのは1回）。"""
    slack = FakeSlack({"C0": "kei-agent"})
    assistant = Assistant(config, store, slack, None, "xoxb", "UBOT")
    down = ': POST /data_sources/abc/query: 503 {"code": "service_unavailable"}'
    await assistant.notify_trouble("Daily の材料を Notion から読めませんでした" + down)
    await assistant.notify_trouble("Retro & Planning を日別記録に保存できませんでした" + down)
    posts = [kw for name, kw in slack.calls if name == "chat_postMessage"]
    updates = [kw for name, kw in slack.calls if name == "chat_update"]
    assert len(posts) == 1 and len(updates) == 1
    assert "（2件）（どれも Notion が一時的に応答しなかったため" in updates[0]["text"]
    assert "• Retro & Planning を日別記録に保存できませんでした" in updates[0]["text"]
    # 時間がたてば、新しい1通にする
    assistant._trouble["at"] -= assistant_module.TROUBLE_GROUP_SECONDS
    await assistant.notify_trouble("ほかの問題")
    assert len([1 for name, _ in slack.calls if name == "chat_postMessage"]) == 2
