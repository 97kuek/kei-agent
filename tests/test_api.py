"""モジュールの差し込み口と窓口（kei_agent.api）。利用者が作ったモジュールが、コアを直さずに動くこと。"""

import json
from dataclasses import replace

import pytest
from fakes import FakeAI, FakeHub, FakeNotion, make_assistant

from kei_agent.api import Request
from kei_agent.execution import a2a, runner
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler, task_names
from kei_agent.testing.kit import settle

MEMO_TOML = '''api = 2
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

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        if req.text == "こわして":
            raise RuntimeError("こわれた")
        ts = await self.core.post(req.channel, "📌 " + req.text)
        self.core.records.put("memo", ts, {"channel": req.channel, "text": req.text, "pinned": False})
        await self.core.reply(req, "メモしたよ")

    async def run_schedule(self, name: str, day: str) -> dict:
        return {"status": "done", "count": len(self.core.records.items("memo"))}

    async def head_action(self, name: str, params: dict) -> dict | None:
        if name != "memo_pin":
            return None
        key = str(params.get("id") or "")
        if self.core.records.get("memo", key) is None:
            raise ValueError("メモがありません")
        pinned = params.get("pinned")
        if not isinstance(pinned, bool):
            raise ValueError("pinned は真偽値にしてください")
        return self.core.records.update("memo", key, pinned=pinned)
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
                                      notion=FakeNotion(), hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack




async def post_memo(assistant, text="牛乳を買う"):
    assistant.remember_message("C50", "50.1", "owner", text, "50.1")
    await assistant.submit(Request("C50", "memo", "50.1", "50.1", text))
    await settle(assistant)


class FakeAgent:
    """担当プロセスの代わり。頼まれた中身を seen に残し、決めた返事を返す。"""
    base_url = "http://fake-agent/memo"

    def __init__(self, state, reply):
        self.state, self.reply, self.seen = state, reply, []

    async def stream(self, skill, text="", params=None, on_progress=None):
        self.seen.append((skill, params, json.loads(text)))
        return a2a.TaskResult(state=self.state, text=json.dumps(self.reply))


async def test_a_user_module_answers_in_its_workspace(env, config):
    _, assistant, slack = env
    await post_memo(assistant)
    pinned, answer = slack.posted()
    assert (pinned["channel"], pinned["text"], pinned["unfurl_links"]) == ("C50", "📌 牛乳を買う", False)
    assert (answer["thread_ts"], answer["text"]) == ("50.1", "メモしたよ")
    assert not (config.research_root / "memo").exists()


async def test_a_broken_module_says_so_instead_of_staying_silent(env, store):
    scheduler, assistant, slack = env
    await post_memo(assistant, "こわして")
    assert any(kw.get("thread_ts") == "50.1" and kw["text"] for kw in slack.posted())
    assert any("#memo の依頼の処理が落ちました" in text for text in slack.texts())


async def test_a_user_module_runs_its_schedule_and_handles_mcp_operations(env, store):
    """利用者のモジュールの定期処理と MCP 操作が、コアを直さずに使える。"""
    scheduler, assistant, slack = env
    await post_memo(assistant)

    assert task_names(assistant.config) == ("night", "toggl_import", "tidy", "daily",
                                           "review", "maintenance")
    assert await scheduler.run_task("tidy", "2026-09-27") == {"status": "done", "count": 1}

    memo_ts = next(row["key"] for row in store.module_records("memo", "memo"))

    answer = await assistant.module_head_action("memo_pin", {"id": memo_ts, "pinned": True})
    assert answer["pinned"] is True
    await assistant.module_head_action("memo_pin", {"id": memo_ts, "pinned": False})
    assert assistant.cores["memo"].records.get("memo", memo_ts)["pinned"] is False
    for params in ({"id": "missing", "pinned": True}, {"id": memo_ts, "pinned": "yes"}):
        with pytest.raises(ValueError):
            await assistant.module_head_action("memo_pin", params)
    assert assistant.cores["memo"].records.get("memo", memo_ts)["pinned"] is False


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


async def test_a_failed_mcp_operation_leaves_other_operations_available(env, monkeypatch):
    _, assistant, _ = env
    await post_memo(assistant)
    original = assistant.modules["memo"].head_action

    async def broken(name, params):
        if name == "memo_broken":
            raise RuntimeError("こわれた")
        return await original(name, params)

    monkeypatch.setattr(assistant.modules["memo"], "head_action", broken)
    with pytest.raises(RuntimeError, match="こわれた"):
        await assistant.module_head_action("memo_broken", {})
    key = assistant.cores["memo"].records.items("memo")[0]
    memo_ts = next(row["key"] for row in assistant.store.module_records("memo", "memo"))
    await assistant.module_head_action("memo_pin", {"id": memo_ts, "pinned": True})
    assert key["text"] == "牛乳を買う" and assistant.cores["memo"].records.get("memo", memo_ts)["pinned"] is True


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
