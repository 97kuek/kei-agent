"""テストで使う偽物。Slack・AI・Notion・ジョブの偽物は kei_agent.testing にある（モジュールを作る人も使う）。
ここには、このリポジトリのテストだけが使うもの（GitHub・依頼のファイル・研究テーマ）を置く。
"""

import json
import re
from pathlib import Path

from kei_agent.conversation import ask
from kei_agent.execution.jobs import REQUESTS_DIR
from kei_agent.testing.fakes import (  # noqa: F401  テストはここからまとめて読む
    FakeAI,
    FakeHub,
    FakeNotion,
    FakeNotionAPI,
    FakePueue,
    FakeSlack,
    check_notion_body,
)
from kei_agent.workspaces import themes


def make_assistant(config, store, channels: dict[str, str] | None = None, *, pueue=None, **kwargs):
    """偽の Slack とジョブの列（pueue の偽物）で Assistant を作り、(assistant, slack) を返す。

    `channels` は FakeSlack に渡すチャンネル ID と名前。ジョブの偽物をテストで見るときは `pueue` に渡す。
    残りの引数（notion・hub・team_url など）は Assistant にそのまま渡す。
    """
    from kei_agent.conversation.assistant import Assistant
    from kei_agent.execution.jobs import JobManager

    slack = FakeSlack(channels or {})
    jobs = JobManager(config, store, pueue if pueue is not None else FakePueue())
    return Assistant(config, store, slack, jobs, "xoxb-test", "UBOT", **kwargs), slack


def write_request(cwd: Path, **payload) -> Path:
    d = cwd / REQUESTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{payload['request_id']}.json"
    path.write_text(json.dumps(payload))
    return path


def _gh_flags(args: tuple[str, ...]) -> dict[str, str]:
    """gh の `--name value` の組を拾う。"""
    return {args[i][2:]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")}


class FakeGitHub:
    """自己改善のモジュールの issues.gh の代わり。本物の GitHub（公開リポジトリ）には届かない。conftest がすべてのテストで差し替える。

    `fail` に操作の先頭2語（`issue create` など）と例外を入れると、その操作だけ失敗させられる。
    """

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.fail: dict[str, Exception] = {}
        self._number = 0

    async def __call__(self, config, *args: str) -> str:
        self.calls.append(args)
        error = self.fail.get(" ".join(args[:2]))
        if error is not None:
            raise error
        if args[:2] == ("issue", "create"):
            self._number += 1
            return f"https://github.com/97kuek/kei-agent/issues/{self._number}\n"
        return ""

    def created(self) -> list[dict[str, str]]:
        """作った issue の title・body・label。"""
        return [_gh_flags(args) for args in self.calls if args[:2] == ("issue", "create")]

    def closed(self) -> list[tuple[str, str, str]]:
        """閉じようとした issue の番号と、添えたコメントと、理由（`--reason`）。"""
        return [(args[2], _gh_flags(args).get("comment", ""), _gh_flags(args).get("reason", ""))
                for args in self.calls if args[:2] == ("issue", "close")]


def pending_asks(config) -> list[tuple[Path, dict]]:
    """置かれている依頼（kei_agent.conversation.ask が書いたファイル）を古い順に。本番は claim_asks で拾う。"""
    directory = ask.ask_dir(config)
    return [(path, json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(directory.glob("*.json"))] if directory.is_dir() else []


def make_theme(config, name="vlm", keywords=("vision language model counting",)):
    ws = themes.resolve(config, name)
    themes.ensure_workspace(ws)
    if keywords is not None:
        md = ws.cwd / "AGENTS.md"
        text = md.read_text().replace("## 検索キーワード\n", "## 検索キーワード\n\n" + "\n".join(f"- {k}" for k in keywords) + "\n", 1)
        md.write_text(text)
    return ws


_TABLE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]")
_TOP_KEY = re.compile(r"^\s*(modules|research_root|course_root)\s*=")


def _without_tables(text: str) -> str:
    """TOML の文から、表に書くもの（modules・research_root・course_root の行と、[channels]・[agents]・[notion] の表）を
    消す。テストは設定を config.toml の形で書き、write_config がそれを表に分ける。"""
    out: list[str] = []
    skipping = False
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        row = lines[i]
        if table := _TABLE.match(row):
            name = table.group(1).strip()
            skipping = name in ("channels", "agents", "notion") or name.startswith(("agents.", "notion."))
        if skipping:
            i += 1
            continue
        if _TOP_KEY.match(row) and not any(_TABLE.match(r) for r in lines[:i]):
            # 複数の行にまたがる配列（modules = [ …）は、閉じ括弧の行まで消す
            if "[" in lines[i].split("#", 1)[0]:
                while "]" not in lines[i].split("#", 1)[0] and i + 1 < len(lines):
                    i += 1
            i += 1
            continue
        out.append(row)
        i += 1
    return "".join(out)


def _without_keys(text: str, drop: dict[str, set[str]]) -> str:
    """TOML の文から、表ごとの決まったキーの行を消す（1行で書いた値だけ。"" は一番外側）。"""
    out, table = [], ""
    for line in text.splitlines(keepends=True):
        if header := _TABLE.match(line):
            table = header.group(1).strip()
        key = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=", line)
        if key and key.group(1) in drop.get(table, ()):
            continue
        out.append(line)
    return "".join(out)


def write_config(path: Path, text: str) -> Path:
    """config.toml を書く。テストでは config.toml の形でまとめて書いてよく、モジュール・チャンネル・AI・作業場・
    Notion のホーム（modules・[channels]・[agents]・[notion]・research_root・course_root）は同じフォルダの
    agents.csv に、定期処理の時刻は schedules.csv に分けて書く。"""
    import tomllib

    from kei_agent.configuration import agents_table, schedules_table
    from kei_agent.framework import modules

    # 利用者のモジュール（同じフォルダの modules/）も、表の行にできるように読んでおく
    modules.register_user_modules(path.parent / "modules")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        data = {}
    if any(key in data for key in agents_table.REPLACED_KEYS):
        (path.parent / agents_table.AGENTS_FILE).write_text(agents_table.from_config(data), encoding="utf-8")
        text = _without_tables(text)
    # 定期処理の時刻とオンオフは schedules.csv に（[schedule] の時刻と [maintenance] の time・enabled）
    known = schedules_table.known_names()
    schedule, maintenance = data.get("schedule", {}), data.get("maintenance", {})
    times = {key: value for key, value in schedule.items() if key in known}
    if times or {"time", "enabled"} & set(maintenance):
        rows = [f"{name},{'true' if value else 'false'},{value}" for name, value in times.items()]
        if {"time", "enabled"} & set(maintenance):
            time = maintenance.get("time", schedules_table.CORE_TIMES["maintenance"])
            on = maintenance.get("enabled", True) and bool(time)
            rows.append(f"maintenance,{'true' if on else 'false'},{time}")
        (path.parent / schedules_table.SCHEDULES_FILE).write_text("name,enabled,time\n" + "\n".join(rows) + "\n",
                                                                  encoding="utf-8")
        text = _without_keys(text, {"schedule": set(times), "maintenance": {"time", "enabled"}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path

