"""テストで使う偽物。"""

import json
from pathlib import Path

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
