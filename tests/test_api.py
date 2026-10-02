"""モジュールの差し込み口と窓口（kei_agent.api）。利用者が作ったモジュールが、コアを直さずに動くこと。"""

import json
from dataclasses import replace

import pytest
from fakes import FakeAI, FakeHub, FakeNotion, make_assistant

from kei_agent.execution import a2a, runner
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler, task_names
from kei_agent.testing.kit import settle

MEMO_TOML = '''api = 1
name = "memo"
label = "メモ"

[channels]
memo = ["memo"]

[schedules.tidy]
label = "メモの整理"
default = "06:00"
'''

MEMO_CODE = '''from kei_agent.api import Core, Request

from . import texts


class Module:
    default_question = "メモを見せて"

    def __init__(self, core: Core):
        self.core = core

    def welcome(self) -> str:
        return texts.WELCOME

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        if req.text == "こわして":
            raise RuntimeError("こわれた")
        ts = await self.core.post(req.channel, "📌 " + req.text)
        self.core.records.put("memo", ts, {"channel": req.channel, "text": req.text, "pinned": False})
        await self.core.reply(req, "メモしたよ")

    async def run_schedule(self, name: str, day: str) -> dict:
        return {"status": "done", "count": len(self.core.records.items("memo"))}

    async def on_reaction(self, event: dict, added: bool) -> bool:
        item = event.get("item") or {}
        if event.get("reaction") != "pushpin" or self.core.records.get("memo", item.get("ts")) is None:
            return False
        self.core.records.update("memo", item["ts"], pinned=added)
        return True
'''


@pytest.fixture
def env(config, store, tmp_path, monkeypatch):
    root = tmp_path / "user-modules"
    (root / "memo").mkdir(parents=True)
    (root / "memo" / "module.toml").write_text(MEMO_TOML, encoding="utf-8")
    (root / "memo" / "module.py").write_text(MEMO_CODE, encoding="utf-8")
    (root / "memo" / "texts.py").write_text('WELCOME = "ここに書いたことをメモするよ。"\n', encoding="utf-8")
    modules.register_user_modules(root)
    config = replace(config, modules=(*config.modules, "memo"),
                     module_channels={**config.module_channels, "memo": ("memo",)})
    monkeypatch.setattr(runner, "run_model", FakeAI())
    channels = {"C1": "vlm", "C5": "0-overview", "C9": "0-kei-agent", "C50": "5-memo"}
    assistant, slack = make_assistant(config, store, channels,
                                      notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack




async def post_memo(assistant, text="牛乳を買う"):
    await assistant.on_mention({"channel": "C50", "user": "UME", "ts": "50.1", "text": f"<@UBOT> {text}"})
    await settle(assistant)


class FakeAgent:
    """担当プロセスの代わり。頼まれた中身を seen に残し、決めた返事を返す。"""
    base_url = "http://fake-agent/memo"

    def __init__(self, state, reply):
        self.state, self.reply, self.seen = state, reply, []

    async def stream(self, skill, text="", params=None, on_progress=None):
        self.seen.append((skill, params, json.loads(text)))
        return a2a.TaskResult(state=self.state, text=json.dumps(self.reply))


async def test_a_user_module_answers_in_its_channel_and_is_introduced(env, config):
    scheduler, assistant, slack = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C50"})
    assert slack.texts() == ["Kei Agent です。このチャンネルの用事はメモエージェントに取り次ぎます。\n"
                             "ここに書いたことをメモするよ。"]
    assert not (config.research_root / "memo").exists()         # 研究テーマにはしない

    await post_memo(assistant)

    pinned, answer = slack.posted()[1:]
    assert (pinned["channel"], pinned["text"], pinned["unfurl_links"]) == ("C50", "📌 牛乳を買う", False)
    assert (answer["thread_ts"], answer["text"]) == ("50.1", "メモしたよ")
    # 依頼の 👀 は ✅ に変わる
    reactions = [(name, kw["name"]) for name, kw in slack.calls if name.startswith("reactions_")]
    assert reactions == [("reactions_add", "eyes"), ("reactions_remove", "eyes"), ("reactions_add", "white_check_mark")]


async def test_a_broken_module_says_so_instead_of_staying_silent(env, store):
    scheduler, assistant, slack = env
    await post_memo(assistant, "こわして")
    assert any(kw.get("name") == "warning" for name, kw in slack.calls if name == "reactions_add")
    assert any("#memo の依頼の処理が落ちました" in text for text in slack.texts())


async def test_a_user_module_runs_its_schedule_and_owns_its_reactions(env, store):
    """利用者のモジュールの定期処理は決まった順に並び、自分の投稿へのリアクションは自分に届く。"""
    scheduler, assistant, slack = env
    await post_memo(assistant)

    assert task_names(assistant.config) == ("night", "literature", "reading", "toggl_import", "tidy", "daily",
                                           "review", "maintenance")
    assert await scheduler.run_task("tidy", "2026-09-27") == {"status": "done", "count": 1}

    memo_ts = next(row["key"] for row in store.module_records("memo", "memo"))

    event = {"reaction": "pushpin", "user": "UME", "item_user": "UBOT",
             "item": {"type": "message", "channel": "C50", "ts": memo_ts}}
    await assistant.on_reaction_added(event)
    assert assistant.modules["memo"].core.records.get("memo", memo_ts)["pinned"] is True
    await assistant.on_reaction_removed(event)
    assert assistant.modules["memo"].core.records.get("memo", memo_ts)["pinned"] is False


async def test_a_module_without_an_ai_can_ask_its_process(env):
    """AI の実行役（[actor]）を持たないモジュールも、担当プロセスに頼める（AI を選ばないので provider を渡さない）。"""
    scheduler, assistant, slack = env
    agent = assistant.agents["memo"] = FakeAgent("TASK_STATE_COMPLETED", {"ok": True, "text": "はい", "data": {}})
    reply = await assistant.cores["memo"].ask_agent("count", {"n": 1})
    assert (reply.ok, reply.text, agent.seen) == (True, "はい", [("count", {}, {"n": 1})])


async def test_the_reason_an_agent_gave_reaches_the_trouble_channel(env):
    """担当が断った理由の頭が、改善のチャンネルの1行に残る（URL や細かい中身はログだけ）。"""
    scheduler, assistant, slack = env
    assistant.agents["memo"] = FakeAgent("TASK_STATE_FAILED", {"ok": False, "data": {}, "text": (
        "arXiv を読めません: https://export.arxiv.org/api/query?x を読めません: HTTP Error 406: Not Acceptable")})
    assert not (await assistant.cores["memo"].ask_agent("count", {})).ok
    notice = slack.texts()[-1]
    assert notice.endswith("メモの担当の count がうまくいかなかった（arXiv を読めません、HTTP 406）")
    assert "export.arxiv.org" not in notice


async def test_one_broken_reaction_hook_does_not_stop_the_others(env, monkeypatch):
    """あるモジュールのリアクションの処理が落ちても、ほかのモジュールには届き、落ちたことは知らせる。"""
    scheduler, assistant, slack = env

    async def broken(event, added):
        raise RuntimeError("こわれた")

    monkeypatch.setattr(assistant.modules["knowledge"], "on_reaction", broken)
    seen = []

    async def memo(event, added):
        seen.append(event["reaction"])
        return True

    monkeypatch.setattr(assistant.modules["memo"], "on_reaction", memo)
    await assistant.on_reaction_added({"reaction": "pushpin", "user": "UME",
                                       "item": {"type": "message", "channel": "C50", "ts": "1.1"}})
    assert seen == ["pushpin"]
    assert any("モジュール「knowledge」がリアクションを扱えませんでした" in text for text in slack.texts())


async def test_notice_once_and_themes_through_the_core(env, config):
    from fakes import make_theme

    scheduler, assistant, slack = env
    core = assistant.modules["memo"].core
    assert core.notice_once("no-db") and not core.notice_once("no-db")
    assert assistant.modules["knowledge"].core.notice_once("no-db")      # 目印はモジュールごと
    make_theme(config, "vlm", keywords=("counting",))
    theme, = core.themes()
    assert (theme.name, theme.keywords, theme.path) == ("vlm", ("counting",), config.research_root / "vlm")
    assert "# テーマ: vlm" in theme.premises
    assert core.channels("memo") == ("memo",) and core.is_owner("UME") and not core.is_owner("USOMEONE")
