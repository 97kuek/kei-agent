import asyncio
from pathlib import Path

import pytest

from ezra import runner
from ezra.assistant import Assistant, history_prompt, split_text
from ezra.jobs import JobManager
from fakes import FakePueue, write_request


class FakeSlack:
    def __init__(self, channels: dict[str, str]):
        self.channels = channels
        self.calls: list[tuple[str, dict]] = []
        self.replies: list[dict] = []
        self._ts = 1000

    def _next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000"

    def posted(self) -> list[dict]:
        return [kw for name, kw in self.calls if name == "chat_postMessage"]

    def texts(self) -> list[str]:
        return [kw.get("text") or kw.get("markdown_text") for kw in self.posted()]

    async def conversations_info(self, channel):
        return {"channel": {"name": self.channels[channel]}}

    async def chat_postMessage(self, **kw):
        self.calls.append(("chat_postMessage", kw))
        return {"ts": self._next_ts()}

    async def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return {}

    async def reactions_add(self, **kw):
        self.calls.append(("reactions_add", kw))

    async def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))

    async def conversations_replies(self, **kw):
        return {"messages": self.replies}

    async def chat_getPermalink(self, **kw):
        return {"permalink": "https://slack.example/p1"}


class FakeClaude:
    """runner.run_claude の代わり。呼ばれた内容を記録し、決めた結果を返す。"""

    def __init__(self):
        self.calls = []
        self.behaviors = []

    async def __call__(self, config, ws, prompt, session_id, channel, thread_ts, on_activity=None):
        self.calls.append({"cwd": ws.cwd, "prompt": prompt, "session_id": session_id, "thread_ts": thread_ts})
        behavior = self.behaviors.pop(0) if self.behaviors else {}
        if on_activity:
            await on_activity("Bash: テスト")
        if "side_effect" in behavior:
            behavior["side_effect"](ws.cwd)
        result = runner.RunResult(
            session_id=behavior.get("session_id", "sess-1"),
            text=behavior.get("text", "結果です"),
            is_error=behavior.get("is_error", False),
            errors=behavior.get("errors", []),
        )
        return result


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C9": "assistant-improve", "C5": "research-overview"})
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
    assert slack.texts()[0].startswith("⏳")
    assert slack.posted()[-1] == {"channel": "C1", "thread_ts": "10.1", "markdown_text": "結果です"}
    updates = [kw["text"] for name, kw in slack.calls if name == "chat_update"]
    assert updates[-1].startswith("✅")
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


async def test_improve_channel_records_backlog(env, backlog_config, store):
    assistant, slack, claude, pueue = env
    assistant.config = backlog_config

    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    backlog = (backlog_config.repo_root / "docs" / "backlog.md").read_text()
    assert "経過をもっと細かく" in backlog and "https://slack.example/p1" in backlog
    assert claude.calls == []
    assert slack.texts()[-1] == "要望を `docs/backlog.md` に記録しました。"


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
    assert slack.texts()[-1] == "集計しました"
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
