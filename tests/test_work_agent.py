"""仕事のモジュール（modules/work/）: 担当プロセス（Outlook の予定）と、本体側の見せ方。"""

import asyncio
import json
import socket
from datetime import datetime

import pytest

from kei_agent.a2a import Agent
from kei_agent_modules.work import module as work

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
    """今日・明日・そのあとに分け、件名と時間と場所とリンクを出す（1行ずつ並べる形）。明日を聞かれたら明日だけ。"""
    events = work.events_of({"items": EVENTS})
    lines = work.events_text(events, datetime(2026, 9, 21, 9, 0)).splitlines()
    assert lines[0] == "*今日*"
    assert lines[1] == "• 10:00–10:15 朝会（Zoom） <https://outlook/1|Outlook>"
    assert lines[2] == "*明日*" and "14:00–15:00 定例" in lines[3]
    assert lines[4] == "*このあと*" and lines[5] == "• 9/25（金） 終日 全社イベント（本社）"

    tomorrow = work.events_text(events, datetime(2026, 9, 21, 9), period="tomorrow")
    assert tomorrow.startswith("*明日*\n• 14:00–15:00 定例")
    assert "朝会" not in tomorrow and "全社イベント" not in tomorrow
    # 予定が無ければそう言い、読めない時刻の予定は落とす
    assert work.events_text([], datetime(2026, 9, 21)) == work.NO_EVENTS
    assert work.events_of({"items": [{"subject": "壊れている", "start": "いつか"}]}) == []


def test_requested_period_preserves_tomorrow_intent():
    assert work.requested_period("明日の予定を教えて") == "tomorrow"
    assert work.requested_period("今日の予定") == "today"
    assert work.requested_period("今週の予定") == "week"


@pytest.fixture
async def server(config, monkeypatch):
    """仕事エージェントを立てる（会社の連携は偽物）。"""
    import uvicorn

    from kei_agent import modules
    from kei_agent_a2a import launch
    from kei_agent_modules.work import connector
    from kei_agent_modules.work.agent import Executor

    asked = []

    async def events(cfg, store, days=7, today=None, provider=""):
        asked.append(days)
        return EVENTS

    monkeypatch.setattr(connector, "events", events)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    app = launch.build_app(modules.builtin()["work"], base, TOKEN, executor=Executor(config))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield base, asked
    server.should_exit = True
    await task


async def test_card_and_list_events_come_back_in_the_envelope(server, monkeypatch):
    from kei_agent_modules.work import connector

    base, asked = server
    card = await Agent(base, TOKEN).card()
    assert card["name"] == "Kei Agent（仕事）"
    assert [s["id"] for s in card["skills"]] == ["list-events", "ask"]

    task = await Agent(base, TOKEN, timeout=30).ask("list-events", json.dumps({"days": 3}))
    envelope = json.loads(task.answer)
    assert task.ok and envelope["ok"] is True
    assert envelope["data"]["days"] == 3 and len(envelope["data"]["items"]) == 3
    assert asked == [3]

    # 連携が使えないときは、その理由を返す（黙って0件にしない）
    async def broken(cfg, store, days=7, today=None, provider=""):
        raise connector.WorkCalendarError("連携を使えませんでした")

    monkeypatch.setattr(connector, "events", broken)
    task = await Agent(base, TOKEN, timeout=30).ask("list-events")
    assert not task.ok and "連携を使えませんでした" in json.loads(task.answer)["text"]


# 会社の Claude アカウントに付いている連携から読む道（既定）


def test_connector_drops_the_body_of_an_event():
    """会議の本文（参加リンクなど）は持ち込まない。"""
    from kei_agent_modules.work import connector

    event = connector._event({"subject": "定例", "start": "2026-09-25T11:00", "end": "2026-09-25T13:00",
                              "location": "Teams", "organizer": "c@example.com", "body": "参加リンク"})
    assert set(event) == {"id", "subject", "start", "end", "all_day", "location", "organizer", "free", "url"}
    assert event["start"] == "2026-09-25T11:00"
    assert event["id"] == ""


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


async def test_calendar_is_read_in_one_read_only_turn_of_the_shared_runner(config, store, monkeypatch):
    """予定の一覧も、共通の起動口で「読むだけ」の1回として動かす（会話は続けない）。"""
    from kei_agent import runner
    from kei_agent_modules.work import connector

    seen = {}

    async def run_model(_config, request, prompt, **_kwargs):
        seen.update(request=request, prompt=prompt)
        return runner.RunResult(text='[{"subject": "定例", "start": "2026-09-25T11:00"}]')

    monkeypatch.setattr(runner, "run_model", run_model)
    events = await connector.events(config, store, 7, provider="claude")

    request = seen["request"]
    assert [e["subject"] for e in events] == ["定例"]
    assert request.read_only and request.session_id is None
    assert (request.recipe.actor, request.recipe.provider) == ("work", "claude")
    assert request.workspace.cwd == config.state_dir / "agents" / "work"
    assert "mcp__" not in seen["prompt"]                              # どちらの provider にも通じる言い方


async def test_calendar_failure_keeps_the_limit(config, store, monkeypatch):
    from kei_agent import runner
    from kei_agent_modules.work import connector

    async def run_model(*_args, **_kwargs):
        return runner.RunResult(is_error=True, errors=["hit your session limit"], limit_reset_at=123.0)

    monkeypatch.setattr(runner, "run_model", run_model)
    with pytest.raises(connector.WorkCalendarError) as caught:
        await connector.events(config, store, 7, provider="claude")
    assert caught.value.limit_reset_at == 123.0
