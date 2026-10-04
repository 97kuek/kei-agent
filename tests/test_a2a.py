"""オーケストレーター（Kei Agent 本体）と、大学エージェント（A2A サーバー）の往復。

本物のサーバーを 127.0.0.1 に立てて、名刺を読み、仕事を頼んで、結果が返るところまでを見る。
"""

import json

import pytest
from fakes import free_port, serving

from kei_agent.execution.a2a import A2AError, Agent

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
pytest.importorskip("uvicorn")

TOKEN = "test-token"


def build_app(base_url: str, token: str):
    """大学のモジュールの担当（共通の起動コマンドが作るのと同じアプリ）。"""
    from kei_agent.framework import modules
    from kei_agent_a2a import launch

    return launch.build_app(modules.builtin()["course"], base_url, token)


@pytest.mark.parametrize("token", ["", "   "])
def test_a2a_app_refuses_to_start_without_a_password(token):
    with pytest.raises(RuntimeError, match="KEI_AGENT_A2A_TOKEN"):
        build_app("http://127.0.0.1:8787", token)


@pytest.fixture
async def server():
    """大学エージェントを立てて、住所を返す。"""
    port = free_port()
    async with serving(build_app(f"http://127.0.0.1:{port}", TOKEN), port) as base:
        yield base


@pytest.fixture
async def stranger():
    """同じポートで待っている、A2A ではない誰か（JSON を返さない）。"""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    port = free_port()

    async def card(request):
        # 名刺だけは読めるが、仕事の窓口は JSON を返さない
        interface = {"url": f"http://127.0.0.1:{port}/a2a", "protocolBinding": "JSONRPC"}
        return PlainTextResponse(json.dumps({"supportedInterfaces": [interface]}), media_type="application/json")

    async def hello(request):
        return PlainTextResponse("<html>Not an agent</html>")

    app = Starlette(routes=[Route("/.well-known/agent-card.json", card),
                            Route("/{path:path}", hello, methods=["GET", "POST"])])
    async with serving(app, port) as base:
        yield base


async def test_a_reply_that_is_not_json_is_an_a2a_error(stranger):
    """別のものが同じポートにいても、ValueError を素で投げず A2AError にする。"""
    with pytest.raises(A2AError, match="SendMessage の返事が JSON ではありません"):
        await Agent(stranger, TOKEN).ask("list-due")
    with pytest.raises(A2AError, match="名刺が JSON ではありません"):
        await Agent(stranger + "/nope", TOKEN).card()


async def test_card_tells_what_the_agent_can_do(server):
    card = await Agent(server, TOKEN).card()
    assert card["name"].startswith("Kei Agent")
    assert [s["id"] for s in card["skills"]] == [
        "sync-assignments", "sync-submissions", "list-due", "list-calendar-assignments", "list-classes",
        "list-current-courses", "time-report"]
    # 自由な質問（ask）は claude を動かすので、流しながら返す
    assert card["capabilities"].get("streaming") is True
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


async def test_card_is_public_but_work_needs_the_password(server):
    assert (await Agent(server).card())["name"].startswith("Kei Agent")
    with pytest.raises(A2AError):
        await Agent(server, "違う合言葉").ask("list-due")


async def test_asking_a_skill_comes_back_with_an_answer(server, monkeypatch):
    """締切の一覧が JSON で返る（見せ方はオーケストレーターが決める）。"""
    from kei_agent_modules.course import notion_sync

    monkeypatch.setattr(notion_sync, "list_calendar_assignments", lambda days, today: {
        "complete": True, "items": [
            {"id": "p1", "moodle_id": "1@moodle", "title": "第3回レポート", "course": "データベース",
             "due": "2099-09-25T23:59:00+09:00", "status": "In progress", "url": "https://notion.example/p1"},
            {"id": "done", "title": "提出済み", "due": "2099-09-24T23:59:00+09:00", "status": "Submitted"},
            {"id": "past", "title": "期限切れ", "due": "2000-09-24T23:59:00+09:00", "status": "Not started"},
        ]})

    # 日数などの指定は、本体（module.py）が本文の JSON で渡す
    result = await Agent(server, TOKEN).ask("list-due", json.dumps({"days": 30}))
    assert result.ok and result.task_id
    # 返事は全エージェント共通の封筒。中身は data に入る
    envelope = json.loads(result.answer)
    assert envelope["ok"] is True and "1 件" in envelope["text"]
    payload = envelope["data"]
    assert payload["days"] == 30 and payload["more"] == 0
    item, = payload["items"]
    assert item["id"] == "1@moodle" and item["course"] == "データベース"
    assert item["at"].startswith("2099-09-25T23:59")


@pytest.mark.parametrize("skill", ["sync-assignments"])
async def test_moodle_skills_say_what_is_missing_without_the_calendar_url(server, monkeypatch, skill):
    monkeypatch.delenv("MOODLE_ICS_URL", raising=False)
    result = await Agent(server, TOKEN).ask(skill)
    assert not result.ok and "カレンダーをエクスポート" in json.loads(result.answer)["text"]


async def test_calendar_assignments_pagination_failure_is_a_failed_task(server, monkeypatch):
    from kei_agent.storage.notion import NotionError
    from kei_agent_modules.course import notion_sync

    def broken_snapshot(days, today):
        raise NotionError("課題 DB の次ページを読めません")

    monkeypatch.setattr(notion_sync, "list_calendar_assignments", broken_snapshot)
    result = await Agent(server, TOKEN).ask("list-calendar-assignments", json.dumps({"days": 30}))

    assert not result.ok
    assert "次ページ" in json.loads(result.answer)["text"]
    assert "TOKEN" not in result.answer


async def test_course_refuses_ai_questions(server, monkeypatch):
    from kei_agent_a2a import run

    async def unexpected(*args, **kwargs):
        pytest.fail("大学の機械処理から AI を呼び出してはいけない")

    monkeypatch.setattr(run.runner, "run_model", unexpected)
    result = await Agent(server, TOKEN).stream("ask", text=json.dumps({"prompt": "情報Bの過去問ある？"}))
    assert not result.ok and "どの仕事か分かりません" in json.loads(result.answer)["text"]


async def test_unknown_skill_fails_with_a_reason(server):
    result = await Agent(server, TOKEN).ask("", text="よろしく")
    assert not result.ok and "どの仕事か分かりません" in json.loads(result.answer)["text"]


async def test_list_classes_for_another_weekday_uses_that_days_date(server, monkeypatch):
    """曜日を指定されたら、今日ではなく、次のその曜日の日付で学期と時刻を決める。"""
    from datetime import date

    from kei_agent_modules.course import agent, notion_sync, school

    class _Today(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 25)   # 金曜

    seen = []

    def courses_on(weekday, day, school=None):
        seen.append((weekday, day))
        return [{"id": "p1", "subject": "データベース", "weekday": weekday, "term": "秋学期", "period": 2, "url": ""}]

    monkeypatch.setattr(agent, "date", _Today)
    monkeypatch.setattr(notion_sync, "courses_on", courses_on)
    # 時限の時刻は、設定の [course] school で選んだ学校の部品から（ここでは早稲田）
    monkeypatch.setattr(agent, "from_config", lambda _config: school.load({"school": "waseda"}))
    result = await Agent(server, TOKEN).ask("list-classes", json.dumps({"weekday": "月"}, ensure_ascii=False))

    assert seen == [("月", date(2026, 9, 28))]
    item, = json.loads(result.answer)["data"]["items"]
    assert item["start"] == "2026-09-28T10:40"


async def test_list_classes_without_period_times_says_how_to_set_them(server, monkeypatch):
    """学校を選んでいなければ時刻は空にして、設定の書き方を添える（朝の一覧には時刻のある授業だけが並ぶ）。"""
    from kei_agent_modules.course import agent, notion_sync, school

    monkeypatch.setattr(notion_sync, "courses_on", lambda weekday, day, school=None: [
        {"id": "p1", "subject": "データベース", "weekday": weekday, "term": "秋学期", "period": 2, "url": ""}])
    monkeypatch.setattr(agent, "from_config", lambda _config: school.School())
    result = await Agent(server, TOKEN).ask("list-classes", json.dumps({"weekday": "月"}, ensure_ascii=False))

    envelope = json.loads(result.answer)
    assert envelope["data"]["items"][0]["start"] == "" and "[course] に school か periods" in envelope["text"]
