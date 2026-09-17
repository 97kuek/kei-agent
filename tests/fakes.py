"""テストで使う偽物。"""

import json
from pathlib import Path

from ezra import runner
from ezra.jobs import REQUESTS_DIR


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

    async def reactions_add(self, **kw):
        self.calls.append(("reactions_add", kw))

    async def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))

    async def conversations_replies(self, **kw):
        return {"messages": self.replies}

    async def conversations_list(self, **kw):
        channels = [{"id": cid, "name": name, "is_member": True} for cid, name in self.channels.items()]
        return {"channels": channels, "response_metadata": {"next_cursor": ""}}

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
