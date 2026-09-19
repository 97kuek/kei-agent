"""テストで使う偽物。"""

import json
from pathlib import Path

from ezra import runner
from ezra.jobs import REQUESTS_DIR
from ezra.notion import NotionError
from ezra.notion_store import Note, Task


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

    async def chat_startStream(self, **kw):
        self.calls.append(("chat_startStream", kw))
        return {"ts": self._next_ts()}

    async def chat_appendStream(self, **kw):
        self.calls.append(("chat_appendStream", kw))
        return {}

    async def chat_stopStream(self, **kw):
        self.calls.append(("chat_stopStream", kw))
        return {}

    def statuses(self) -> list[str]:
        return [kw["status"] for name, kw in self.calls if name == "agents_sessions_setStatus"]

    def streamed(self) -> list[str]:
        """流して見せた文章。start と append を順につないだもの。"""
        return [kw["markdown_text"] for name, kw in self.calls
                if name in ("chat_startStream", "chat_appendStream") and kw.get("markdown_text")]

    async def reactions_add(self, **kw):
        self.calls.append(("reactions_add", kw))

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
    """runner.run_claude の代わり。呼ばれた内容を記録し、決めた結果を返す。"""

    def __init__(self):
        self.calls = []
        self.behaviors = []

    async def __call__(self, config, ws, prompt, session_id, channel, thread_ts, on_activity=None, on_text=None):
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

    def add_task(self, title, theme=None, status="今夜やる", slack_url=None, body="", assignee="Ezra"):
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
        return [t for t in self.tasks.values() if t.assignee == "Ezra" and t.status == "今夜やる"][:limit]

    def count_tonight_tasks(self):
        self._check()
        return len([t for t in self.tasks.values() if t.assignee == "Ezra" and t.status == "今夜やる"])

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
