"""モジュール1つを、偽物の Slack・AI・Notion・担当と一緒に本体の中で動かす（ModuleKit）。

    kit = ModuleKit(tmp_path / "memo", tmp_path)    # モジュールのフォルダ（組み込みなら名前でもよい）
    kit.ai.answer("メモしたよ")                       # AI が次に返す答え
    await kit.message("牛乳を買う")                   # このモジュールのチャンネルで、依頼者が @Kei Agent に頼む
    assert kit.texts()[-1] == "メモしたよ"
    kit.close()                                      # 差し替えを戻す（with ModuleKit(...) as kit: でもよい）

pytest なら plugin（kei_agent.testing.plugin）の module_kit fixture が作り、終わったら戻す。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from kei_agent import agents_table, model_classifier, modules, runner
from kei_agent.agents import Reply
from kei_agent.assistant import Assistant
from kei_agent.config import load_config, model_actors
from kei_agent.jobs import JobManager
from kei_agent.schedule import Scheduler
from kei_agent.store import Store
from kei_agent.testing.agents import FakeAgent, LocalAgent
from kei_agent.testing.fakes import FakeAI, FakeHub, FakeNotion, FakePueue, FakeSlack
from kei_agent.testing.isolation import (
    REAL_CLASSIFY,
    Patches,
    guard_state,
    strip_secrets,
    stub_classifier,
    stub_restarts,
)

# 依頼者（KEI_AGENT_ALLOWED_USER_ID）と、Kei Agent 自身の Slack の ID
OWNER = "UME"
BOT = "UBOT"
# 置き場所（テストの一時フォルダの下に作る）
PLACES = ("agent_root", "state_dir")
# 研究と大学の置き場所（担当の表の folder 列に書く）
FOLDERS = ("research_root", "course_root")


async def settle(assistant: Assistant) -> None:
    """裏で動かした仕事が全部終わるまで待つ（終わった仕事を1つずつ待つと、集合から外れる前に空回りすることがある）。"""
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks), return_exceptions=True)
        await asyncio.sleep(0)


def toml_value(value) -> str:
    """設定に書く値（文字・数・真偽・配列・表）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)} = {toml_value(v)}"
                                for k, v in value.items()) + " }"
    raise TypeError(f"設定に書けない値です: {value!r}")


class ModuleKit:
    """モジュール1つを、本番と同じ読み方の設定と、偽物の Slack・AI・Notion・担当と一緒に動かす。

    - module: モジュールのフォルダ（利用者のモジュール）か、組み込みの名前
    - others: いっしょにオンにするモジュール（頼るモジュール [depends] requires は、書かなくてもオンにする）
    - settings: このモジュールの設定（config.toml の [名前]）。config: 設定に足すもの（{"channels": {...}} は担当の表へ）
    - local_agent: 担当プロセス（[process]）を、同じプロセスの中で agent.py の Executor で動かす（LocalAgent）。
      False か、a2a-sdk が入っていなければ FakeAgent（返事は kit.agent.reply で並べる）

    テストのあいだ、本物の秘密情報を環境変数から外し、利用者のフォルダ（KEI_AGENT_HOME）を一時フォルダにし、
    本物の状態・launchd・用途の分類器に触れないようにする（close で戻す）。
    """

    def __init__(self, module: str | Path, tmp_path: Path, *, others: Iterable[str] = (), settings: dict | None = None,
                 config: dict | None = None, local_agent: bool = True):
        self.tmp = Path(tmp_path)
        self._patches = Patches()
        self._ts = 5000
        try:
            self._start(module, list(others), settings or {}, config or {}, local_agent)
        except BaseException:
            self._patches.undo()
            raise

    def _start(self, module, others: list[str], settings: dict, extra: dict, local_agent: bool) -> None:
        patch = self._patches
        strip_secrets(patch)
        self.restarted = stub_restarts(patch)
        guard_state(patch, self.tmp / "default-paths")
        if model_classifier.classify_module is REAL_CLASSIFY:
            stub_classifier(patch)
        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        patch.setenv("KEI_AGENT_HOME", str(home))
        self.spec = self._resolve(module, home / "modules")
        self.name = self.spec.name
        names = self._with_requires([self.name, *others])
        (home / "config.toml").write_text(self._config_text(settings, extra), encoding="utf-8")
        # モジュールのオンオフ・チャンネル・AI は担当の表に（AI はどれも claude）
        (home / agents_table.AGENTS_FILE).write_text(agents_table.from_config(
            {"modules": names, "channels": extra.get("channels", {}), **{key: str(self.tmp / key) for key in FOLDERS}},
            {actor: "claude" for actor in model_actors()}), encoding="utf-8")
        self.config = load_config(env={"KEI_AGENT_HOME": str(home), "KEI_AGENT_ALLOWED_USER_ID": OWNER})
        self.store = Store(self.config.db_path)
        self.slack = FakeSlack(self._channel_map())
        self.ai = FakeAI()
        patch.setattr(runner, "run_model", self.ai)
        self.notion, self.hub, self.pueue = FakeNotion(), FakeHub(), FakePueue()
        self.assistant = Assistant(self.config, self.store, self.slack, JobManager(self.config, self.store, self.pueue),
                                   "xoxb-test", BOT, notion=self.notion, team_url="https://example.slack.com/",
                                   hub=self.hub)
        self.scheduler = Scheduler(self.config, self.store, self.assistant)
        # 担当プロセスは、本物の番地（手元で動いている Kei Agent かもしれない）に届かないよう、全部差し替える
        self.agents: dict[str, FakeAgent | LocalAgent] = {name: FakeAgent(name) for name in self.assistant.agents}
        for spec in modules.enabled(self.config.modules):
            if spec.port is not None and not spec.service:
                self.agents[spec.name] = self._agent_for(spec, local_agent)
        self.assistant.agents.clear()
        self.assistant.agents.update(self.agents)

    def _resolve(self, module, user_modules: Path) -> modules.ModuleSpec:
        """モジュールの定義。利用者のモジュールは、本番と同じく利用者のフォルダの modules/ から読む
        （一時フォルダの modules/ に、同じフォルダにあるモジュールをつないでおく。オンにするのは頼んだものだけ）。"""
        # 読んだ利用者のモジュールは、close で前のものに戻す（読むと中身を入れ替えるので、写しに差し替えてから読む）
        self._patches.setattr(modules, "_user", dict(modules._user))
        path = Path(module)
        if path.is_dir() and path.resolve().parent != modules.BUILTIN_DIR:
            user_modules.mkdir(parents=True, exist_ok=True)
            for folder in sorted(path.resolve().parent.iterdir()):
                if (folder / modules.SPEC_FILE).is_file() and not (user_modules / folder.name).exists():
                    (user_modules / folder.name).symlink_to(folder, target_is_directory=True)
        modules.register_user_modules(user_modules)
        name = path.resolve().name if path.is_dir() else str(module)
        spec = modules.known().get(name)
        if spec is None:
            raise ValueError(f"知らないモジュールです: {module}（フォルダの場所か、組み込みの名前を渡す）")
        return spec

    @staticmethod
    def _with_requires(names: list[str]) -> list[str]:
        found: list[str] = []
        todo = list(names)
        while todo:
            name = todo.pop(0)
            if name in found:
                continue
            found.append(name)
            spec = modules.known().get(name)
            todo += list(spec.requires) if spec is not None else []
        return found

    def _config_text(self, settings: dict, extra: dict) -> str:
        lines = [f"{key} = {toml_value(str(self.tmp / key))}" for key in PLACES]
        lines += [f"{key} = {toml_value(value)}" for key, value in extra.items() if not isinstance(value, dict)]
        tables: dict[str, dict] = {}
        if settings:
            tables[self.name] = dict(settings)
        for key, value in extra.items():
            if isinstance(value, dict) and key != "channels":
                tables[key] = {**tables.get(key, {}), **value}
        for table, values in tables.items():
            lines += ["", f"[{table}]", *(f"{json.dumps(k, ensure_ascii=False)} = {toml_value(v)}"
                                          for k, v in values.items())]
        return "\n".join(lines) + "\n"

    def _channel_map(self) -> dict[str, str]:
        """チャンネルの ID → 名前（本体のチャンネルと、オンのモジュールのチャンネル）。"""
        found = {"C_OVERVIEW": self.config.overview_channels[0], "C_KEI_AGENT": self.config.improve_channels[0]}
        for spec in modules.enabled(self.config.modules):
            for kind in spec.channels:
                names = self.config.module_channels.get(kind, ())
                if names == (modules.ALL_CHANNELS,):
                    found["C_THEME"] = "theme"
                elif names:
                    found[f"C_{kind.upper().replace('-', '_')}"] = names[0]
        return found

    def _agent_for(self, spec: modules.ModuleSpec, local: bool) -> FakeAgent | LocalAgent:
        if not local:
            return FakeAgent(spec.name)
        try:
            code = modules.load_agent(spec)
        except ImportError:
            # a2a-sdk（uv sync --group agents）が無ければ、担当は偽物にする
            return FakeAgent(spec.name)
        parameters = inspect.signature(code.Executor).parameters
        executor = code.Executor(self.config, self.store, **({"pueue": self.pueue} if "pueue" in parameters else {}))
        executor.agent = spec.name
        return LocalAgent(executor, code.SKILLS)

    # 窓口

    @property
    def module(self):
        """このモジュールの class Module（本体が作ったもの）。"""
        return self.assistant.modules[self.name]

    @property
    def core(self):
        """このモジュールの窓口（kei_agent.api.Core）。"""
        return self.assistant.cores[self.name]

    @property
    def records(self):
        return self.core.records

    @property
    def agent(self) -> FakeAgent | LocalAgent | None:
        """このモジュールの担当プロセスの代わり（[process] が無ければ None）。"""
        return self.agents.get(self.name)

    # チャンネル

    def channel(self, key: str | None = None) -> str:
        """チャンネルの ID（ID・名前・チャンネルの種類のどれで渡してもよい。無ければこのモジュールのチャンネル）。"""
        if key is None:
            return self._home_channel()
        if key in self.slack.channels:
            return key
        names = [key, *self.config.module_channels.get(key, ())[:1]]
        for channel_id, name in self.slack.channels.items():
            if name in names:
                return channel_id
        raise KeyError(f"知らないチャンネルです: {key}（kit.add_channel で足す）")

    def add_channel(self, name: str) -> str:
        """チャンネルを足して、その ID を返す（研究テーマのチャンネルなど）。"""
        channel_id = f"C_EXTRA{len(self.slack.channels)}"
        self.slack.channels[channel_id] = name
        return channel_id

    def _home_channel(self) -> str:
        for kind, names in self.spec.channels.items():
            return "C_THEME" if names == (modules.ALL_CHANNELS,) else self.channel(kind)
        if self.spec.core_channels:
            return "C_KEI_AGENT"
        raise KeyError(f"モジュール「{self.name}」はチャンネルを持たないので、channel= で渡してください")

    def _next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000100"

    # 依頼者のすること

    async def settle(self) -> None:
        await settle(self.assistant)

    async def message(self, text: str, channel: str | None = None, *, thread: str | None = None) -> str:
        """依頼者が @Kei Agent に頼む（thread を渡すと、そのスレッドの中で）。仕事が終わるまで待ち、依頼の ts を返す。"""
        ts = self._next_ts()
        event = {"channel": self.channel(channel), "user": OWNER, "ts": ts, "text": f"<@{BOT}> {text}"}
        if thread:
            event["thread_ts"] = thread
        await self.assistant.on_mention(event)
        await self.settle()
        return ts

    async def reply(self, text: str, thread: str, channel: str | None = None) -> str:
        """Kei Agent が動いているスレッドに、メンションなしで返信する。"""
        ts = self._next_ts()
        await self.assistant.on_message({"channel": self.channel(channel), "user": OWNER, "ts": ts,
                                         "thread_ts": thread, "text": text})
        await self.settle()
        return ts

    async def invite(self, channel: str | None = None) -> None:
        """Kei Agent をチャンネルに招く（モジュールの案内 welcome が出る）。"""
        await self.assistant.on_member_joined({"user": BOT, "channel": self.channel(channel)})
        await self.settle()

    async def slash(self, command: str, text: str = "", channel: str | None = None) -> str:
        """スラッシュコマンドを打つ（/ は付けない）。打った人にだけ見せる文を返す。"""
        body = {"user_id": OWNER, "channel_id": self.channel(channel), "command": f"/{command}", "text": text,
                "trigger_id": "trigger-1"}
        answer = await self.assistant.module_slash(command, body)
        await self.settle()
        return answer

    async def action(self, name: str, value: str = "", *, channel: str | None = None, message_ts: str = "",
                     **extra) -> None:
        """このモジュールの投稿のボタンを押す（name は core.action_id に渡した名前）。"""
        body = {"user": {"id": OWNER}, "trigger_id": "trigger-1",
                "actions": [{"action_id": self.core.action_id(name), "value": value}],
                "channel": {"id": self.channel(channel)}, "message": {"ts": message_ts}, **extra}
        await self.assistant.module_action(body)
        await self.settle()

    async def view(self, name: str, values: dict, *, private_metadata: str = "") -> dict | None:
        """このモジュールの入力の画面を送る（name は core.view_id に渡した名前）。欄の下に出す理由を返す。"""
        body = {"user": {"id": OWNER}, "view": {"callback_id": self.core.view_id(name), "blocks": [],
                                                 "state": {"values": values}, "private_metadata": private_metadata}}
        errors = await self.assistant.module_view(body)
        await self.settle()
        return errors

    async def schedule(self, name: str, day: str | None = None) -> dict:
        """定期処理を1回動かす（day は YYYY-MM-DD。無ければ今日）。結果を返す。"""
        result = await self.scheduler.run_task(name, day or date.today().isoformat())
        await self.settle()
        return result

    async def tick(self, now: datetime | None = None) -> None:
        """見回り（class Module の tick）を1回動かす。"""
        await self.scheduler.module_ticks(now or datetime.now())
        await self.settle()

    async def emit(self, kind: str, **fields) -> None:
        """出来事を知らせる（class Module の on_event に届く）。"""
        self.assistant.emit(kind, **fields)
        await self.settle()

    async def home(self) -> dict:
        """依頼者の App Home を描いて、その画面を返す。"""
        await self.assistant.publish_home(OWNER)
        await self.settle()
        return next(kw["view"] for name, kw in reversed(self.slack.calls) if name == "views_publish")

    async def skill(self, name: str, payload: dict | str | None = None, **params) -> Reply:
        """担当プロセス（agent.py）の仕事を直接頼む（本体の core.ask_agent と同じ形。provider は既定で claude）。"""
        if not isinstance(self.agent, LocalAgent):
            raise RuntimeError(f"モジュール「{self.name}」の担当を、同じプロセスの中で動かしていません"
                               "（[process] と agent.py、a2a-sdk が要る）")
        text = payload if isinstance(payload, str) else json.dumps(payload or {}, ensure_ascii=False)
        return Reply.of(await self.agent.stream(name, text, {"provider": "claude", **params}))

    # 見るもの

    def texts(self) -> list[str]:
        """Slack に出した文（投稿と、流して見せた返事。順に）。"""
        return [message["text"] for message in self.slack.messages()]

    def thread(self, ts: str) -> list[str]:
        """そのスレッドに返した文（投稿と、流して見せた返事。順に）。"""
        return [message["text"] for message in self.slack.messages() if message["thread_ts"] == ts]

    # 終わり

    def close(self) -> None:
        """差し替えた環境変数と部品を元に戻す。"""
        self._patches.undo()

    def __enter__(self) -> ModuleKit:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
