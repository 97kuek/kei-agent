"""モジュールの枠の広がり（段階3の①）。仕事の担当を載せ替える前に、利用者のモジュールで確かめる。

連携の道具、用途の選び分け、skill とフックの置き場所、研究全体のチャンネルからの振り分け、予定（agenda）、声からの問い合わせ。
"""

import json
from dataclasses import replace
from datetime import datetime

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack

from kei_agent.conversation import a2a, router
from kei_agent.conversation.assistant import Assistant
from kei_agent.execution import model_classifier, model_policy, runner
from kei_agent.execution.agent_policy import policy_of
from kei_agent.execution.jobs import JobManager
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler
from kei_agent.testing.kit import settle

CALENDAR_TOML = '''api = 1
name = "calendar"
label = "予定"

[actor]
prompt = "calendar.md"
plugin = true
default_use_case = "calendar_list"
classify = "予定の一覧は calendar_list、予定の組み方の相談は calendar_plan。"

[[actor.connectors]]
name = "google-calendar"
claude_server = "claude_ai_Google_Calendar"
claude_tools = ["list_events", "get_event"]

[[actor.connectors.codex_apps]]
name = "Google Calendar"
namespace = "google_calendar"
tools = ["list_events"]

[use_cases.calendar_list]
claude = { model = "claude-sonnet-5", effort = "medium" }

[use_cases.calendar_plan]
claude = { model = "claude-sonnet-5", effort = "high" }

[process]
port = 8801

[channels]
calendar = ["calendar"]
'''

CALENDAR_CODE = '''from kei_agent.api import ASK, Core, Request, failure_text


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.routed = []

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        if not skill:
            skill, params = await self.core.pick_skill(req)
        self.routed.append((skill, params))
        if skill == ASK:
            await self.core.converse(req)
            return
        reply = await self.core.ask_agent("list-events", {"days": (params or {}).get("days", 7)})
        if not reply.ok:
            await self.core.reply(req, failure_text(), failed=True)
            return
        await self.core.reply(req, f"{len(reply.data.get('items') or [])} 件")

    async def agenda(self, days: int, kinds=None):
        reply = await self.core.ask_agent("list-events", {"days": days})
        if not reply.ok:
            return None
        return [{"kind": "meeting", "source": "Google", **item} for item in reply.data.get("items") or []]
'''

AGENT_CODE = '''from kei_agent_a2a.api import ASK, AgentSkill, SkillExecutor

SKILLS = [AgentSkill(id="list-events", name="予定", description="予定の一覧", tags=["calendar"]),
          AgentSkill(id=ASK, name="質問", description="予定の質問", tags=["calendar"])]


class Executor(SkillExecutor):
    async def handle(self, updater, metadata, text):
        await self.done(updater, "0 件", {"items": []})
'''

EVENTS = [{"id": "g1", "subject": "打ち合わせ", "start": "2026-09-28T10:00", "end": "2026-09-28T11:00",
           "location": "会議室A", "url": "https://calendar.example/g1"}]


def calendar_module(root):
    folder = root / "calendar"
    (folder / "plugin" / ".claude-plugin").mkdir(parents=True)
    (folder / "plugin" / ".claude-plugin" / "plugin.json").write_text('{"name": "calendar"}', encoding="utf-8")
    (folder / "module.toml").write_text(CALENDAR_TOML, encoding="utf-8")
    (folder / "module.py").write_text(CALENDAR_CODE, encoding="utf-8")
    (folder / "agent.py").write_text(AGENT_CODE, encoding="utf-8")
    (folder / "calendar.md").write_text("# 予定の担当\n", encoding="utf-8")
    return folder


class FakeCalendarAgent:
    """予定の担当の偽物。list-events は決めておいた予定を、ask は答えを返す。"""
    base_url = "http://127.0.0.1:8801"

    def __init__(self, items=None, broken=False):
        self.items = EVENTS if items is None else items
        self.broken = broken
        self.asked: list[tuple[str, dict, dict]] = []

    async def card(self):
        return {"skills": [{"id": "list-events", "description": "予定の一覧"}, {"id": "ask", "description": "質問"}]}

    async def stream(self, skill, text="", params=None, on_progress=None):
        payload = json.loads(text) if text.startswith("{") else {}
        self.asked.append((skill, payload, dict(params or {})))
        if self.broken:
            return a2a.TaskResult(state="TASK_STATE_FAILED", text=json.dumps(
                {"ok": False, "text": "読めなかった", "data": {}, "limit_reset_at": None, "cost_usd": None}))
        data = ({"items": self.items} if skill == "list-events"
                else {"text": "<<kei-agent-final>>\n答えだよ\n<<kei-agent-final-end>>", "session_id": "s1"})
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps(
            {"ok": True, "text": "済", "data": data, "limit_reset_at": None, "cost_usd": None}, ensure_ascii=False))


@pytest.fixture
def env(config, store, tmp_path, monkeypatch):
    root = tmp_path / "user-modules"
    calendar_module(root)
    modules.register_user_modules(root)
    config = replace(config, modules=(*config.modules, "calendar"),
                     module_channels={**config.module_channels, "calendar": ("calendar",)},
                     agent_profiles={**config.agent_profiles, "calendar": config.agent_profiles["work"]})
    slack = FakeSlack({"C1": "vlm", "C5": "0-overview", "C9": "0-kei-agent", "C60": "6-calendar"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
    agent = FakeCalendarAgent()
    assistant.agents["calendar"] = agent
    return Scheduler(config, store, assistant), assistant, slack, claude, agent




# module.toml

def test_connectors_become_the_only_account_tools_the_actor_gets(env):
    policy = policy_of("calendar")
    connector, = policy.connectors
    assert connector.claude_names() == ("mcp__claude_ai_Google_Calendar__list_events",
                                        "mcp__claude_ai_Google_Calendar__get_event")
    app, = policy.codex_apps
    assert (app.name, app.tools) == ("Google Calendar", ("google_calendar.list_events",))
    # 振り分け・分類の回は、どの担当のものでも道具を持たない
    assert policy_of("calendar", model_policy.UseCase.ROUTING).connectors == ()
    # plugin はモジュールのフォルダに置く
    assistant = env[1]
    assert assistant.config.agent_plugin_dir("calendar") == modules.known()["calendar"].path / "plugin"


@pytest.mark.parametrize(("extra", "message"), [
    ("[[actor.connectors]]\nname = \"x\"\n", "claude_server"),
    ("[[actor.connectors]]\nname = \"x\"\nclaude_server = \"s\"\nclaude_tools = [\"a b\"]\n", "道具の名前"),
    # classify は用途が2つ以上あるときだけ、plugin はフォルダがあるときだけ
    ('classify = "迷ったら broken_answer"\n', "classify"),
    ("plugin = true\n", "plugin.json"),
])
def test_a_broken_actor_is_refused(tmp_path, extra, message):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "broken.md").write_text("#\n", encoding="utf-8")
    (folder / "module.toml").write_text(
        'api = 1\nname = "broken"\n[actor]\nprompt = "broken.md"\n' + extra
        + '[use_cases.broken_answer]\nclaude = { model = "claude-sonnet-5" }\n', encoding="utf-8")
    with pytest.raises(modules.ModuleError, match=message):
        modules.load_spec(folder)


# 用途の選び分け

async def test_a_module_can_choose_its_use_case_with_the_light_classifier(env, fake_model_classifier, monkeypatch):
    scheduler, assistant, slack, claude, _ = env
    monkeypatch.setattr(model_classifier, "classify_module", fake_model_classifier)      # ここでは本物で選ぶ
    claude.behaviors = [{"text": '{"use_case": "calendar_plan", "confidence": 0.9}'}]

    use_case = await model_classifier.classify(assistant.config, assistant.store, "calendar", "来週の予定を組みたい",
                                               provider="claude")

    assert use_case == "calendar_plan"
    prompt = claude.calls[0]["prompt"]
    assert "calendar_list, calendar_plan" in prompt and "予定の組み方の相談は calendar_plan" in prompt
    recipe = model_policy.resolve_classifier(assistant.config, assistant.store, "calendar", provider="claude")
    model_policy.validate_resolved(recipe)                        # 分類の recipe は、モジュールの担当でも通る
    # 自信が無ければ default_use_case
    claude.behaviors = [{"text": '{"use_case": "calendar_plan", "confidence": 0.5}'}]
    assert await model_classifier.classify(assistant.config, assistant.store, "calendar", "？",
                                           provider="claude") == "calendar_list"


# 振り分けと差し込み口

async def test_the_module_picks_its_own_skill_in_its_channel(env, monkeypatch):
    scheduler, assistant, slack, claude, agent = env

    async def fake_pick(config, skills, text, *, store=None):
        assert [s["id"] for s in skills] == ["list-events", "ask"]
        return router.Choice(skill="list-events", params={"days": 2})

    monkeypatch.setattr(router, "pick", fake_pick)
    await assistant.on_mention({"channel": "C60", "user": "UME", "ts": "60.1", "text": "<@UBOT> 明日の予定は？"})
    await settle(assistant)

    assert assistant.modules["calendar"].routed == [("list-events", {"days": 2})]
    skill, payload, params = agent.asked[0]
    assert (skill, payload, params) == ("list-events", {"days": 2}, {"provider": "claude"})
    assert slack.texts()[-1] == "1 件"

    # 担当が失敗したら、依頼に ⚠️ を付けて知らせる
    agent.broken = True
    await assistant.on_mention({"channel": "C60", "user": "UME", "ts": "60.2", "text": "<@UBOT> 予定は？"})
    await settle(assistant)
    assert "接続に失敗" in slack.texts()[-1]
    assert ("reactions_add", {"channel": "C60", "timestamp": "60.2", "name": "warning"}) in slack.calls


async def test_a_choice_from_the_overview_router_reaches_the_module(env, monkeypatch):
    """研究全体のチャンネルで振り分け係が選んだ仕事は、選び直さずにモジュールへ渡る。"""
    scheduler, assistant, slack, claude, agent = env

    async def fake_pick_across(config, catalog, text, *, store=None):
        assert "calendar" in catalog
        return router.Choice(agent="calendar", skill="list-events", params={"days": 7})

    async def no_pick(*args, **kwargs):
        raise AssertionError("選び直さない")

    monkeypatch.setattr(router, "pick_across", fake_pick_across)
    monkeypatch.setattr(router, "pick", no_pick)
    await assistant.on_mention({"channel": "C5", "user": "UME", "ts": "5.1", "text": "<@UBOT> 今週の予定は？"})
    await settle(assistant)

    assert assistant.modules["calendar"].routed == [("list-events", {"days": 7})]


# 予定（agenda）

async def test_module_agenda_reaches_the_morning_summary_and_the_calendar(env):
    scheduler, assistant, slack, claude, agent = env

    text, detail, _ = await scheduler.morning_text(datetime(2026, 9, 28, 8, 0))

    assert "`10:00–11:00` 💼 打ち合わせ（会議室A）" in text
    assert detail["agenda"]["synced"]["Google"] == {"created": 1, "updated": 0, "stale": 0}
    row, = assistant.hub.calendar
    assert (row["出典"], row["出典 ID"], row["名前"]) == ("Google", "g1", "打ち合わせ")


async def test_an_unreadable_agenda_is_told_and_never_marks_the_calendar(env):
    scheduler, assistant, slack, claude, agent = env
    assistant.hub.calendar.append({"id": "cal-0", "出典": "Google", "出典 ID": "g0", "名前": "前からある会議",
                                   "日付": "2026-09-29T10:00", "同期状態": "確認済み"})
    agent.broken = True
    told = []

    async def notify(text):
        told.append(text)
    assistant.notify_trouble = notify

    text, detail, _ = await scheduler.morning_text(datetime(2026, 9, 28, 8, 0))

    assert detail["agenda"] == {"synced": {}, "unread": ["予定"]}
    assert assistant.hub.calendar[0]["同期状態"] == "確認済み"          # 読めなかった日に「要確認」にしない
    # 読めなかったことは、朝の一覧ではなく #0-kei-agent に知らせる
    assert "予定の予定の読み取り" not in text and any("予定の予定の読み取り" in t for t in told)


# 声

async def test_voice_can_ask_a_module_actor_read_only(env, monkeypatch):
    scheduler, assistant, slack, claude, agent = env
    asked = []

    async def fake_ask_agent(actor, prompt, session_id=None, channel="", thread_ts="", on_activity=None, *,
                             provider=None, read_only=False, use_case=None):
        asked.append((actor, read_only, use_case))
        return runner.RunResult(text="<<kei-agent-final>>\n明日は打ち合わせ\n<<kei-agent-final-end>>")

    monkeypatch.setattr(assistant, "ask_agent", fake_ask_agent)
    answer = await assistant.answer_question("calendar", "明日の予定は？")
    assert answer == "明日は打ち合わせ" and asked == [("calendar", True, "calendar_list")]


# 見回り（tick）

async def test_module_ticks_run_every_minute_and_one_failure_does_not_stop_the_others(env, monkeypatch):
    scheduler, assistant, slack, claude, agent = env
    seen = []

    async def calendar_tick(now):
        seen.append(("calendar", now))

    async def broken_tick(now):
        raise RuntimeError("こわれた")

    monkeypatch.setattr(assistant.modules["calendar"], "tick", calendar_tick, raising=False)
    monkeypatch.setattr(assistant.modules["knowledge"], "tick", broken_tick, raising=False)
    now = datetime(2026, 9, 28, 9, 0)
    await scheduler.module_ticks(now)
    assert seen == [("calendar", now)]

    async def offline_tick(now):
        raise ConnectionError("名前を引けない")

    monkeypatch.setattr(assistant.modules["calendar"], "tick", offline_tick, raising=False)
    with pytest.raises(ConnectionError):          # ネットにつながらないことは、safe_tick に任せる
        await scheduler.module_ticks(now)


# 予定カレンダー（core.sync_calendar）

async def test_a_module_writes_its_own_rows_to_the_hub_calendar(env):
    scheduler, assistant, slack, claude, agent = env
    core = assistant.modules["calendar"].core
    assistant.hub.calendar.append({"id": "cal-0", "出典": "手入力", "出典 ID": "", "名前": "自分で入れた予定",
                                   "日付": "2026-09-29", "同期状態": ""})
    items = [{"id": "a1", "title": "レポート", "start": "2026-10-25", "status": "未着手"}]

    report = await core.sync_calendar("課題", items, day="2026-09-27", days=400, complete=True, expected_count=1)

    assert report == {"created": 1, "updated": 0, "stale": 0}
    assert [(row["出典"], row["名前"]) for row in assistant.hub.calendar] == [("手入力", "自分で入れた予定"),
                                                                           ("課題", "レポート")]
    # 数が合わない（読み損ねた）ときは、何も書かない
    assert await core.sync_calendar("課題", items, day="2026-09-27", days=400, complete=True,
                                    expected_count=2) == "error"
    assistant.hub = None
    assert await core.sync_calendar("課題", items, day="2026-09-27", days=400, complete=True) == "no_hub"


# 授業と締切（agenda の種類）、取り込み（prepare）

def _school_items(now):
    today = now.date().isoformat()
    return [{"kind": "class", "subject": "データベース", "start": f"{today}T10:40", "end": f"{today}T12:20"},
            {"kind": "due", "id": "r1", "title": "第3回レポート", "course": "データベース",
             "at": f"{today}T17:00", "notice": "due:r1"}]


async def test_classes_and_dues_from_a_module_reach_the_morning_summary(env, monkeypatch):
    scheduler, assistant, slack, claude, agent = env
    now = datetime(2026, 9, 28, 7, 30)
    asked = []

    async def agenda(days, kinds=None):
        asked.append((days, kinds))
        return [item for item in _school_items(now) if kinds is None or item["kind"] in kinds]

    async def prepare(kind, day):
        return ["課題の取り込み"]

    module = assistant.modules["calendar"]
    monkeypatch.setattr(module, "agenda", agenda)
    monkeypatch.setattr(module, "prepare", prepare, raising=False)

    text, detail, notices = await scheduler.morning_text(now)

    assert "`10:40–12:20` 🎓 データベース" in text and "⏰ 締切: データベース 第3回レポート" in text
    assert "うまくいかなかった" not in text and "─" not in text       # 一覧には予定だけ
    # 朝に出した締切は、そのモジュールの目印で記録する（24時間前の知らせで繰り返さない）
    assert notices == ["module.calendar.due:r1"]
    assert asked == [(7, None)]

    # 振り返りは締切だけを頼む（会議は読まない）
    agenda_items, _ = await assistant.module_agenda(3, frozenset({"due"}))
    assert asked[-1] == (3, frozenset({"due"}))
    assert [item["kind"] for item in agenda_items["calendar"]] == ["due"]


async def test_module_agenda_goes_into_the_review_material(env, monkeypatch):
    from kei_agent.scheduling import digest

    scheduler, assistant, slack, claude, agent = env
    builder = digest.DigestBuilder(assistant.config, assistant.store, assistant)
    lines = await builder._agenda(datetime(2026, 9, 27, 21, 0).timestamp())
    assert "## 予定" in lines and "- 明日の予定: 10:00–11:00 打ち合わせ" in lines

    # 授業と締切は、今日あったものとして並ぶ
    now = datetime(2026, 9, 28, 21, 0)

    async def agenda(days, kinds=None):
        return _school_items(now)

    monkeypatch.setattr(assistant.modules["calendar"], "agenda", agenda)
    lines = await builder._agenda(now.timestamp())
    assert "- 今日あった予定: 10:40–12:20 データベース" in lines
    assert "- 今日が期限だったもの: データベース / 第3回レポート（17:00）" in lines


def test_an_agenda_without_kinds_is_refused_at_startup(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / "module.toml").write_text('api = 1\nname = "old"\n', encoding="utf-8")
    (folder / "module.py").write_text("class Module:\n    async def agenda(self, days):\n        return []\n",
                                      encoding="utf-8")
    with pytest.raises(modules.ModuleError, match="agenda"):
        modules.load_code(modules.load_spec(folder))
