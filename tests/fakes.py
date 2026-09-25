"""テストで使う偽物。"""

import json
from pathlib import Path

from kei_agent import runner
from kei_agent.jobs import REQUESTS_DIR
from kei_agent.notion import NotionError
from kei_agent.notion_store import Note, Task


class FakePueue:
    def __init__(self):
        self.added = []
        self.killed = []
        self.removed = []
        self.task_status: dict[int, dict] = {}

    async def add(self, cwd, command, label):
        task_id = len(self.added)
        self.added.append((cwd, command, label))
        self.task_status[task_id] = {"status": {"Queued": {"enqueued_at": "2026-09-17T10:00:00+09:00"}}}
        return task_id

    async def kill(self, task_id):
        self.killed.append(task_id)

    async def remove(self, task_id):
        self.removed.append(task_id)
        self.task_status.pop(task_id, None)

    async def tasks(self):
        return dict(self.task_status)


def write_request(cwd: Path, **payload) -> Path:
    d = cwd / REQUESTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{payload['request_id']}.json"
    path.write_text(json.dumps(payload))
    return path


class FakeSlack:
    def __init__(self, channels: dict[str, str]):
        self.channels = channels
        self.calls: list[tuple[str, dict]] = []
        self.replies: list[dict] = []
        self.stream_modes: dict[str, str] = {}
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

    # AI アプリ向けの表示

    async def agents_sessions_setStatus(self, **kw):
        self.calls.append(("agents_sessions_setStatus", kw))
        return {}

    async def assistant_threads_setStatus(self, **kw):
        self.calls.append(("assistant_threads_setStatus", kw))
        return {}

    async def chat_startStream(self, **kw):
        self.calls.append(("chat_startStream", kw))
        ts = self._next_ts()
        self.stream_modes[ts] = "chunks" if kw.get("chunks") else "markdown_text"
        return {"ts": ts}

    async def chat_appendStream(self, **kw):
        # 実物と同じく、始めたときと違う形（chunks と markdown_text）を混ぜると断る
        mode = "chunks" if kw.get("chunks") else "markdown_text"
        if self.stream_modes.get(kw["ts"], mode) != mode:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("streaming_mode_mismatch", {"ok": False, "error": "streaming_mode_mismatch"})
        self.calls.append(("chat_appendStream", kw))
        return {}

    async def chat_stopStream(self, **kw):
        self.calls.append(("chat_stopStream", kw))
        return {}

    def statuses(self) -> list[str]:
        return [kw["status"] for name, kw in self.calls if name == "agents_sessions_setStatus"]

    def thinking(self) -> list[str]:
        """「〇〇が入力中」の欄に出した文言。"""
        return [kw["status"] for name, kw in self.calls if name == "assistant_threads_setStatus"]

    def tasks(self) -> list[dict]:
        """流して見せた返事の中の、作業の手順（task_update）。"""
        return [c for name, kw in self.calls if name in ("chat_startStream", "chat_appendStream", "chat_stopStream")
                for c in kw.get("chunks") or [] if c["type"] == "task_update"]

    def streamed(self) -> list[str]:
        """流して見せた文章。start と append を順につないだもの。"""
        texts = []
        for name, kw in self.calls:
            if name in ("chat_startStream", "chat_appendStream", "chat_stopStream"):
                if kw.get("markdown_text"):
                    texts.append(kw["markdown_text"])
                texts += [c["text"] for c in kw.get("chunks") or [] if c["type"] == "markdown_text"]
        return texts

    async def reactions_add(self, **kw):
        self.calls.append(("reactions_add", kw))

    async def reactions_remove(self, **kw):
        self.calls.append(("reactions_remove", kw))

    async def views_publish(self, **kw):
        self.calls.append(("views_publish", kw))
        return {}

    async def views_open(self, **kw):
        self.calls.append(("views_open", kw))
        return {}

    async def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))

    async def conversations_replies(self, **kw):
        return {"messages": self.replies}

    async def conversations_list(self, **kw):
        channels = [{"id": cid, "name": name, "is_member": True} for cid, name in self.channels.items()]
        return {"channels": channels, "response_metadata": {"next_cursor": ""}}

    async def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://example.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}


class FakeClaude:
    """runner.run_model の代わり。解決済み execution request を記録して返す。"""

    def __init__(self):
        self.calls = []
        self.behaviors = []

    async def __call__(self, config, request, prompt, on_activity=None, on_text=None):
        ws = request.workspace
        self.calls.append({"cwd": ws.cwd, "prompt": prompt, "session_id": request.session_id,
                           "thread_ts": request.thread_ts})
        behavior = self.behaviors.pop(0) if self.behaviors else {}
        # 途中の独り言と道具の呼び出し。実物の claude と同じく、独り言は道具の直前に来る
        for kind, value in behavior.get("steps", [("tool", "Bash: テスト")]):
            if kind == "text" and on_text:
                await on_text(value)
            elif kind == "tool" and on_activity:
                await on_activity(value)
        if "side_effect" in behavior:
            behavior["side_effect"](ws.cwd)
        # 実物と同じく、最後の result のイベントから組み立てる（上限の読み取りなども同じ道を通る）
        text = behavior.get("text", "結果です")
        if (text and not behavior.get("is_error", False) and not behavior.get("raw", False)
                and "<<kei-agent-final>>" not in text and "<<kei-agent-final-end>>" not in text):
            text = f"<<kei-agent-final>>\n{text}\n<<kei-agent-final-end>>"
        result = runner.RunResult(provider=request.recipe.provider)
        runner.apply_event(result, {
            "type": "result",
            "session_id": behavior.get("session_id", "sess-1"),
            "result": text,
            "is_error": behavior.get("is_error", False),
            "errors": behavior.get("errors", []),
        })
        if on_text and result.text:
            await on_text(result.text)
        return result


class FakeNotion:
    """NotionStore の代わり。Task とノートをメモリに持つ。"""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.bodies: dict[str, str] = {}
        self.results: dict[str, str] = {}
        self.notes: list[Note] = []
        self.appended: list[tuple[str, str]] = []
        self.themes: dict[str, str] = {}
        self.fail = False
        self._n = 0

    def _check(self):
        if self.fail:
            raise NotionError("503 Service Unavailable")

    def add_task(self, title, theme=None, status="今夜やる", slack_url=None, body="", assignee="Kei Agent"):
        self._n += 1
        task = Task(f"task-{self._n}", title, status, assignee, "P1", None, slack_url,
                    [f"theme-{theme}"] if theme else [], f"https://notion.example/task-{self._n}",
                    [theme] if theme else [])
        self.tasks[task.id] = task
        self.bodies[task.id] = body
        return task

    def create_night_task(self, title, theme_name, slack_url, body):
        self._check()
        for t in self.tasks.values():
            if t.slack_url == slack_url:
                t.status = "今夜やる"
                return t
        return self.add_task(title, theme_name, slack_url=slack_url, body=body)

    def cancel_night_task(self, slack_url):
        self._check()
        for t in self.tasks.values():
            if t.slack_url == slack_url and t.status == "今夜やる":
                t.status = "未着手"
                return True
        return False

    def tonight_tasks(self, limit):
        self._check()
        return [t for t in self.tasks.values() if t.assignee == "Kei Agent" and t.status == "今夜やる"][:limit]

    def count_tonight_tasks(self):
        self._check()
        return len([t for t in self.tasks.values() if t.assignee == "Kei Agent" and t.status == "今夜やる"])

    def update_task(self, page_id, status=None, result=None, slack_url=None):
        self._check()
        task = self.tasks[page_id]
        if status:
            task.status = status
        if result is not None:
            self.results[page_id] = result
        if slack_url:
            task.slack_url = slack_url

    def page_markdown(self, page_id):
        return self.bodies.get(page_id, "")

    def ensure_theme(self, name, slack_url, directory):
        self._check()
        if name in self.themes:
            return False
        self.themes[name] = slack_url
        return True

    def notes_edited_since(self, since, kinds):
        self._check()
        return [n for n in self.notes if n.kind in kinds]

    def awaiting_tasks(self):
        self._check()
        return [t for t in self.tasks.values() if t.status == "確認待ち"]

    def tasks_due_on(self, day):
        """その日が期日の Task。済みも返す（Daily で取り消し線にするため）。"""
        return [t for t in self.tasks.values() if t.due == day.isoformat()]

    def tasks_due_within(self, today, days):
        self._check()
        return []

    def upcoming_milestones(self, today, limit=5):
        self._check()
        return [{"name": "中間発表", "due": "2026-10-01", "url": "https://notion.example/m1"}]

    def create_note(self, title, kind, day, markdown, slack_url=None, file=None):
        self._check()
        note = Note(f"note-{len(self.notes) + 1}", title, kind, day, f"https://notion.example/note-{len(self.notes) + 1}",
                    markdown)
        self.notes.append(note)
        return note

    def append_markdown(self, page_id, markdown):
        self._check()
        self.appended.append((page_id, markdown))
