"""担当プロセス（[process] を持つモジュールの agent.py）の代わり。どちらも本体から見れば a2a.Agent と同じ
（stream / ask / card）なので、本体の assistant.agents に置ける。

- FakeAgent … 頼まれた仕事を残し、並べておいた返事を返す（本体側だけを確かめるとき）
- LocalAgent … 同じプロセスの中で agent.py の Executor.handle を呼ぶ（A2A のサーバーを立てずに、本体と担当を通す）
"""

from __future__ import annotations

import json

from kei_agent.a2a import TaskResult

COMPLETED, FAILED = "TASK_STATE_COMPLETED", "TASK_STATE_FAILED"


def _payload(text: str):
    """本体が本文に入れた JSON（入っていなければ文のまま）。"""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _parts_text(message) -> str:
    return "\n".join(str(getattr(part, "text", "") or "") for part in message or []).strip()


class FakeAgent:
    """担当プロセスの偽物。`calls` に（仕事の名前、細かい指定、本文の JSON）を残し、`reply` で並べた返事を返す。"""

    def __init__(self, name: str, skills: list[dict] | None = None):
        self.name = name
        self.base_url = f"http://fake-agent/{name}"
        self.calls: list[tuple[str, dict, object]] = []
        self.skills = list(skills or [])
        self.replies: dict[str, dict] = {}

    def reply(self, skill: str, text: str = "", *, ok: bool = True, data: dict | None = None) -> FakeAgent:
        """その仕事を頼まれたときの返事（担当の封筒と同じ ok・text・data）。"""
        self.replies[skill] = {"ok": ok, "text": text, "data": data or {}}
        return self

    async def stream(self, skill: str, text: str = "", params: dict | None = None, on_progress=None,
                     session=None) -> TaskResult:
        self.calls.append((skill, dict(params or {}), _payload(text)))
        answer = self.replies.get(skill, {"ok": True, "text": "", "data": {}})
        body = json.dumps(answer, ensure_ascii=False)
        return TaskResult(state=COMPLETED if answer["ok"] else FAILED, text=body, status_text=body)

    async def ask(self, skill: str, text: str = "", params: dict | None = None, session=None, on_progress=None,
                  poll_seconds: float = 0) -> TaskResult:
        return await self.stream(skill, text, params, on_progress)

    async def card(self) -> dict:
        return {"skills": list(self.skills)}


class _Updater:
    """A2A の TaskUpdater の代わり（Executor.handle に渡す）。"""

    def __init__(self, on_progress=None):
        self.state = ""
        self.text = ""
        self.progress: list[str] = []
        self._on_progress = on_progress

    def new_agent_message(self, parts):
        return parts

    async def start_work(self, message=None) -> None:
        return None

    async def update_status(self, _state, message=None) -> None:
        text = _parts_text(message)
        self.progress.append(text)
        if self._on_progress is not None and text:
            await self._on_progress(text)

    async def complete(self, message=None) -> None:
        self.state, self.text = "completed", _parts_text(message)

    async def failed(self, message=None) -> None:
        self.state, self.text = "failed", _parts_text(message)

    async def cancel(self, message=None) -> None:
        self.state = "canceled"


class LocalAgent:
    """同じプロセスの中で、担当の Executor（agent.py）に仕事を渡す。`calls` と、最後の仕事の経過 `progress` を残す。"""

    def __init__(self, executor, skills=()):
        self.executor = executor
        self.base_url = f"local://{executor.agent}"
        self.calls: list[tuple[str, dict, object]] = []
        self.progress: list[str] = []
        self._skills = list(skills)

    async def stream(self, skill: str, text: str = "", params: dict | None = None, on_progress=None,
                     session=None) -> TaskResult:
        self.calls.append((skill, dict(params or {}), _payload(text)))
        updater = _Updater(on_progress)
        await self.executor.handle(updater, {"skill": skill} | (params or {}), text or skill)
        self.progress = updater.progress
        state = COMPLETED if updater.state == "completed" else FAILED
        return TaskResult(state=state, text=updater.text, status_text=updater.text)

    async def ask(self, skill: str, text: str = "", params: dict | None = None, session=None, on_progress=None,
                  poll_seconds: float = 0) -> TaskResult:
        return await self.stream(skill, text, params, on_progress)

    async def card(self) -> dict:
        """名刺の仕事の一覧（agent.py の SKILLS。本体の振り分け係が読む形）。"""
        from google.protobuf.json_format import MessageToDict

        return {"skills": [MessageToDict(skill) for skill in self._skills]}
