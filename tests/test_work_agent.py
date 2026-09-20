"""仕事エージェント（Outlook の予定）と、その見せ方。"""

import asyncio
import json
import socket
from datetime import datetime

import pytest

from kei_agent import work
from kei_agent.a2a import Agent

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
pytest.importorskip("uvicorn")

TOKEN = "test-token"

EVENTS = [
    {"subject": "朝会", "start": "2026-09-21T10:00:00.0000000", "end": "2026-09-21T10:15:00.0000000",
     "location": "Zoom", "organizer": "上司", "all_day": False, "url": "https://outlook/1", "id": "1"},
    {"subject": "定例", "start": "2026-09-22T14:00:00.0000000", "end": "2026-09-22T15:00:00.0000000",
     "location": "", "organizer": "先方", "all_day": False, "url": "", "id": "2"},
    {"subject": "全社イベント", "start": "2026-09-25T00:00:00.0000000", "end": "2026-09-26T00:00:00.0000000",
     "location": "本社", "organizer": "総務", "all_day": True, "url": "", "id": "3"},
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_events_are_grouped_by_day():
    """今日・明日・そのあとに分け、件名と時間と場所とリンクだけを出す（本文は持ち出さない）。"""
    text = work.events_text(work.events_of({"items": EVENTS}), datetime(2026, 9, 21, 9, 0))
    lines = text.splitlines()
    assert lines[0] == "*今日*"
    assert lines[1] == "• 10:00–10:15 朝会（Zoom） <https://outlook/1|Outlook>"
    assert lines[2] == "*明日*" and "14:00–15:00 定例" in lines[3]
    assert lines[4] == "*このあと*" and lines[5] == "• 9/25（金） 終日 全社イベント（本社）"


def test_no_events_says_so():
    assert work.events_text([], datetime(2026, 9, 21)) == work.NO_EVENTS


def test_broken_times_are_dropped():
    assert work.events_of({"items": [{"subject": "壊れている", "start": "いつか"}]}) == []


class FakeGraph:
    def __init__(self, events):
        self.events_ = events
        self.asked = []

    def events(self, days=7, since=None):
        self.asked.append(days)
        return self.events_


@pytest.fixture
async def server(monkeypatch):
    """仕事エージェントを立てる（Outlook は偽物）。"""
    import uvicorn

    from kei_agent_work import graph
    from kei_agent_work.app import build_app

    fake = FakeGraph(EVENTS)
    monkeypatch.setattr(graph.Graph, "load", classmethod(lambda cls, **kw: fake))
    # 既定は会社の連携から読む。ここでは Entra ID のアプリ（graph）の道を試す
    monkeypatch.setenv("WORK_CALENDAR_SOURCE", "graph")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(build_app(base, TOKEN), host="127.0.0.1", port=port,
                                           log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield base, fake
    server.should_exit = True
    await task


async def test_card_says_it_reads_the_calendar(server):
    base, _ = server
    card = await Agent(base, TOKEN).card()
    assert card["name"] == "Kei Agent（仕事）"
    assert [s["id"] for s in card["skills"]] == ["list-events"]


async def test_list_events_comes_back_in_the_envelope(server):
    base, fake = server
    task = await Agent(base, TOKEN, timeout=30).ask("list-events", params={"days": 3})
    envelope = json.loads(task.answer)
    assert task.ok and envelope["ok"] is True
    assert envelope["data"]["days"] == 3 and len(envelope["data"]["items"]) == 3
    assert fake.asked == [3]


async def test_without_the_permission_it_says_what_to_run(server, monkeypatch):
    from kei_agent_work import graph

    def _no_token(cls, **kw):
        raise graph.GraphError(graph.NO_TOKEN)

    monkeypatch.setenv("WORK_CALENDAR_SOURCE", "graph")
    monkeypatch.setattr(graph.Graph, "load", classmethod(_no_token))
    base, _ = server
    task = await Agent(base, TOKEN, timeout=30).ask("list-events")
    assert not task.ok and "kei-agent-ms-login" in json.loads(task.answer)["text"]


# 会社の Claude アカウントに付いている連携から読む道（既定）


def test_connector_reads_the_json_and_drops_the_body():
    """連携には JSON で答えさせる。会議の本文（参加リンクなど）は持ち込まない。"""
    from kei_agent_a2a import claude

    text = ('はい、調べました。\n[{"subject": "定例", "start": "2026-09-25T11:00", '
            '"end": "2026-09-25T13:00", "location": "Teams", "organizer": "c@example.com"}]')
    found = claude.json_reply(text)
    assert found[0]["subject"] == "定例"
    from kei_agent_work import connector

    event = connector._event(found[0])
    assert set(event) == {"subject", "start", "end", "all_day", "location", "organizer", "free", "url"}
    assert event["start"] == "2026-09-25T11:00"


def test_connector_says_when_the_reply_is_not_json():
    from kei_agent_a2a import claude

    with pytest.raises(claude.ConnectorError, match="読めません"):
        claude.json_reply("[これは JSON ではない]")
    assert claude.json_reply("予定はありません") == []
