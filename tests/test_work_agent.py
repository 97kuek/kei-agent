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
    """今日・明日・そのあとに分け、件名と時間と場所とリンクを出す（1行ずつ並べる形）。"""
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


@pytest.fixture
async def server(config, monkeypatch):
    """仕事エージェントを立てる（会社の連携は偽物）。"""
    import uvicorn

    from kei_agent_work import connector
    from kei_agent_work.app import build_app
    from kei_agent_work.executor import WorkExecutor

    asked = []

    async def events(cfg, days=7, today=None, store=None):
        asked.append(days)
        return EVENTS

    monkeypatch.setattr(connector, "events", events)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    app = build_app(base, TOKEN, executor=WorkExecutor(config))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield base, asked
    server.should_exit = True
    await task


async def test_card_says_it_reads_the_calendar(server):
    base, _ = server
    card = await Agent(base, TOKEN).card()
    assert card["name"] == "Kei Agent（仕事）"
    assert [s["id"] for s in card["skills"]] == ["list-events", "ask"]


async def test_list_events_comes_back_in_the_envelope(server):
    base, asked = server
    task = await Agent(base, TOKEN, timeout=30).ask("list-events", params={"days": 3})
    envelope = json.loads(task.answer)
    assert task.ok and envelope["ok"] is True
    assert envelope["data"]["days"] == 3 and len(envelope["data"]["items"]) == 3
    assert asked == [3]


async def test_without_the_connection_it_says_so(server, monkeypatch):
    """連携が使えないときは、その理由を返す（黙って0件にしない）。"""
    from kei_agent_work import connector

    async def broken(cfg, days=7, today=None, store=None):
        raise connector.WorkCalendarError("連携を使えませんでした")

    monkeypatch.setattr(connector, "events", broken)
    base, _ = server
    task = await Agent(base, TOKEN, timeout=30).ask("list-events")
    assert not task.ok and "連携を使えませんでした" in json.loads(task.answer)["text"]


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
    with pytest.raises(claude.ConnectorError, match="JSON の配列"):
        claude.json_reply("予定はありません")


def test_calendar_text_escapes_values_from_outlook():
    """予定名や場所を Slack のメンション・リンクとして解釈させない。"""
    events = [{
        "subject": "全社 <!channel> <https://evil.example|開く>",
        "start": "2026-09-21T10:00",
        "end": "2026-09-21T10:30",
        "location": "<@U123>",
        "url": "https://outlook.example/item?x=1|<!channel>",
    }]

    text = work.events_text(events, datetime(2026, 9, 21, 9, 0))

    assert "<!channel>" not in text and "<@U123>" not in text
    assert "&lt;!channel&gt;" in text and "&lt;@U123&gt;" in text
    assert "|&lt;!channel&gt;" not in text


def test_work_connector_has_no_write_tools():
    """仕事は読むだけ。送信・作成・更新の道具を許可の一覧に入れない。"""
    from kei_agent_work import connector

    forbidden = ("send", "create", "update", "delete", "move", "upload", "post")
    assert not any(any(word in name.lower() for word in forbidden) for name in connector.ALLOWED_ASK)
    assert not any(any(word in name.lower() for word in forbidden) for name in connector.ALLOWED)
