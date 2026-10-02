"""モジュールを作る人のためのテストの道具（kei_agent.testing。段階5の①）。"""

import os

import pytest

from kei_agent.framework import modules
from kei_agent.testing import FakeAgent, LocalAgent, ModuleKit
from kei_agent.testing.kit import toml_value

MEMO_TOML = '''api = 1
name = "memo"
label = "メモ"

[channels]
memo = ["memo"]

[slash_commands]
memo = "メモの数を見る"

[schedules.tidy]
label = "メモの整理"
default = "06:00"

[settings]
mark = "📌"
'''

MEMO_CODE = '''from kei_agent.api import Core, Request


class Module:
    def __init__(self, core: Core):
        self.core = core

    def welcome(self) -> str:
        return "ここに書いたことをメモするよ。"

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        if req.text.startswith("まとめて"):
            # req を渡すと、経過と答えをスレッドに出す
            await self.core.run_ai("memo_sum", req.text, req=req)
            return
        ts = await self.core.post(req.channel, self.core.settings["mark"] + " " + req.text,
                                  blocks=[{"type": "actions", "elements": [
                                      {"type": "button", "text": {"type": "plain_text", "text": "消す"},
                                       "action_id": self.core.action_id("forget"), "value": req.text}]}])
        self.core.records.put("memo", ts, {"ts": ts, "text": req.text})
        await self.core.reply(req, "メモしたよ")

    async def on_slash_command(self, name: str, body: dict) -> str:
        return f"メモは {len(self.core.records.items('memo'))} 件"

    async def on_action(self, name: str, body: dict) -> None:
        for row in self.core.records.items("memo"):
            if row["text"] == body["actions"][0]["value"]:
                self.core.records.delete("memo", row["ts"])

    async def run_schedule(self, name: str, day: str) -> dict:
        return {"status": "done", "count": len(self.core.records.items("memo"))}
'''

AI_TOML = MEMO_TOML + '''
[actor]
prompt = "memo.md"
default_use_case = "memo_sum"

[use_cases.memo_sum]
claude = { model = "claude-haiku-4-5" }
codex = { model = "gpt-6-luna", effort = "low" }
'''

ECHO_TOML = '''api = 1
name = "echo"
label = "こだま"

[channels]
echo = ["echo"]

[process]
port = 8899
'''

ECHO_CODE = '''from kei_agent.api import Core, Request


class Module:
    def __init__(self, core: Core):
        self.core = core

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        reply = await self.core.ask_agent("shout", {"text": req.text})
        await self.core.reply(req, reply.text if reply.ok else "担当が答えない", failed=not reply.ok)
'''

ECHO_AGENT = '''import json

from kei_agent_a2a.api import AgentSkill, SkillExecutor

SKILLS = [AgentSkill(id="shout", name="叫ぶ", description="大きな声で返す", tags=["echo"])]


class Executor(SkillExecutor):
    agent = "echo"

    async def handle(self, updater, metadata, text):
        if metadata.get("skill") != "shout":
            await self.fail(updater, "知らない仕事")
            return
        await self.done(updater, json.loads(text)["text"] + "！", {"provider": metadata.get("provider")})
'''


def _module(root, name, files):
    folder = root / name
    folder.mkdir(parents=True)
    for file, text in files.items():
        (folder / file).write_text(text, encoding="utf-8")
    return folder


def _memo(tmp_path):
    return _module(tmp_path / "mine", "memo", {"module.toml": MEMO_TOML, "module.py": MEMO_CODE})


def _echo(tmp_path):
    return _module(tmp_path / "mine", "echo", {"module.toml": ECHO_TOML, "module.py": ECHO_CODE,
                                               "agent.py": ECHO_AGENT})


async def test_a_module_answers_its_channel_its_command_its_button_and_its_schedule(tmp_path, module_kit):
    kit = module_kit(_memo(tmp_path))
    await kit.invite()
    assert kit.texts()[-1].endswith("ここに書いたことをメモするよ。")

    ts = await kit.message("牛乳を買う")
    assert kit.thread(ts) == ["メモしたよ"] and "📌 牛乳を買う" in kit.texts()
    assert await kit.slash("memo") == "メモは 1 件"
    assert await kit.schedule("tidy", "2026-09-27") == {"status": "done", "count": 1}
    await kit.action("forget", "牛乳を買う")
    assert kit.records.items("memo") == []

    # 設定は本番と同じ読み方（config.toml の [memo]）で変えられる
    other = module_kit(tmp_path / "mine" / "memo", settings={"mark": "✏️"})
    await other.message("卵")
    assert "✏️ 卵" in other.texts()


async def test_the_ai_answers_what_was_queued_and_remembers_how_it_was_asked(tmp_path, module_kit):
    folder = _module(tmp_path / "mine", "memo", {"module.toml": AI_TOML, "module.py": MEMO_CODE,
                                                 "memo.md": "メモをまとめる。"})
    kit = module_kit(folder)
    kit.ai.answer("3件のメモ: 牛乳・卵・パン")
    ts = await kit.message("まとめて")
    assert kit.thread(ts) == ["3件のメモ: 牛乳・卵・パン"]
    call, = kit.ai.calls
    assert (call["actor"], call["use_case"], call["provider"]) == ("memo", "memo_sum", "claude")
    # 走らせた記録に、担当・用途・provider・モデル・effort が残る
    run = kit.store.conn.execute("SELECT actor, use_case, provider, model, effort FROM runs").fetchone()
    assert tuple(run) == ("memo", "memo_sum", "claude", "claude-haiku-4-5", None)
    assert kit.ai.prompts() == [call["prompt"]] and "まとめて" in call["prompt"]


async def test_a_module_process_runs_in_the_same_process(tmp_path, module_kit):
    pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
    kit = module_kit(_echo(tmp_path))
    assert isinstance(kit.agent, LocalAgent)
    ts = await kit.message("やっほー")
    assert kit.thread(ts) == ["やっほー！"]
    # AI の実行役を持たないモジュールなので、AI（provider）は渡さない
    assert kit.agent.calls == [("shout", {}, {"text": "やっほー"})]
    # 担当の仕事を直接頼むこともできる。名刺の仕事の一覧も読める
    reply = await kit.skill("shout", {"text": "おーい"})
    assert (reply.ok, reply.text, reply.data) == (True, "おーい！", {"provider": "claude"})
    assert not (await kit.skill("sing")).ok
    assert [skill["id"] for skill in (await kit.agent.card())["skills"]] == ["shout"]


async def test_a_fake_process_answers_what_was_queued(tmp_path, module_kit):
    kit = module_kit(_echo(tmp_path), local_agent=False)
    assert isinstance(kit.agent, FakeAgent)
    kit.agent.reply("shout", "偽物の返事")
    assert kit.thread(await kit.message("やっほー")) == ["偽物の返事"]
    kit.agent.reply("shout", "こわれた", ok=False)
    assert kit.thread(await kit.message("もう一度")) == ["担当が答えない"]
    # 本物の番地の担当は、どれも偽物に置き換わっている
    assert all(isinstance(agent, (FakeAgent, LocalAgent)) for agent in kit.assistant.agents.values())


async def test_a_builtin_module_can_be_named_and_brings_what_it_requires(module_kit):
    pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")
    kit = module_kit("knowledge")
    assert kit.config.modules == ("knowledge",) and isinstance(kit.agent, LocalAgent)
    kit.ai.answer("要点は3つ")
    ts = await kit.message("この記事の要点は？")
    assert kit.thread(ts)[-1] == "要点は3つ" and kit.ai.calls[0]["actor"] == "knowledge"
    # 担当のプロセスで決まった用途とモデルも、封筒で本体に戻って記録に残る
    run = kit.store.conn.execute("SELECT actor, use_case, provider, model, effort FROM runs").fetchone()
    assert tuple(run) == ("knowledge", "knowledge_answer", "claude", "claude-sonnet-5", "medium")


def test_the_kit_keeps_real_things_away_while_it_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("KEI_AGENT_HOME", "/real/home")
    with ModuleKit(_memo(tmp_path), tmp_path / "kit") as kit:
        assert "SLACK_BOT_TOKEN" not in os.environ and "OPENAI_API_KEY" not in os.environ
        assert os.environ["KEI_AGENT_HOME"] == str(tmp_path / "kit" / "home")
        assert kit.config.state_dir.is_relative_to(tmp_path) and kit.config.research_root.is_relative_to(tmp_path)
        assert "memo" in modules.known()
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-real" and os.environ["KEI_AGENT_HOME"] == "/real/home"
    assert "memo" not in modules.known()


def test_unknown_modules_and_channels_say_so(tmp_path, module_kit):
    with pytest.raises(ValueError, match="知らないモジュール"):
        module_kit("nothing")
    kit = module_kit(_memo(tmp_path))
    with pytest.raises(KeyError, match="kit.add_channel"):
        kit.channel("lab")
    assert kit.channel(kit.add_channel("lab")) == kit.channel("lab")


def test_settings_are_written_as_toml():
    assert toml_value({"a": [1, True, "x"], "b": {"c": 1.5}}) == '{ "a" = [1, true, "x"], "b" = { "c" = 1.5 } }'
    with pytest.raises(TypeError):
        toml_value(object())
