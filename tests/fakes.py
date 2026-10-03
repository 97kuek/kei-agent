"""テストで使う偽物。Slack・AI・Notion・ジョブの偽物は kei_agent.testing にある（モジュールを作る人も使う）。
ここには、このリポジトリのテストだけが使うもの（GitHub・依頼のファイル・研究テーマ・本物の HTTP で立てるサーバー）を置く。
"""

import asyncio
import contextlib
import csv
import io
import json
import socket
import subprocess
from collections.abc import Iterable
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
    残りの引数（notion・hub など）は Assistant にそのまま渡す。
    """
    from kei_agent.conversation.assistant import Assistant
    from kei_agent.execution.jobs import JobManager

    slack = FakeSlack(channels or {})
    jobs = JobManager(config, store, pueue if pueue is not None else FakePueue())
    return Assistant(config, store, slack, jobs, **kwargs), slack


def free_port() -> int:
    """127.0.0.1 の空いているポート。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def serving(app, port: int = 0):
    """app を 127.0.0.1 に uvicorn で立て、立ち上がったら住所（`http://127.0.0.1:<port>`）を渡す。

    port が 0 なら空いている番地に立てる。名刺に自分の住所を書くアプリは、先に free_port() で番地を決めて渡す。
    """
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):  # 立ち上がるまで待つ
            if server.started:
                break
            await asyncio.sleep(0.05)
        port = port or server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def make_home(tmp_path: Path, text: str = "", *, agents: str | Iterable[str | dict] | None = None,
              schedules: str | Iterable[str | dict] | None = None) -> Path:
    """tmp_path の下に設定のフォルダ（home）を作り、text を config.toml に書く。agents・schedules を渡すと、
    write_agents・write_schedules で表も書く（渡さなければ表は無い）。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_config(home / "config.toml", text)
    if agents is not None:
        write_agents(home, agents)
    if schedules is not None:
        write_schedules(home, schedules)
    return home


def final_answer(text: str) -> str:
    """AI の返事のうち、利用者に見せる答え（最後の答えの印で囲んだ部分）。"""
    return f"<<kei-agent-final>>\n{text}\n<<kei-agent-final-end>>"


def git(repo: Path, *args: str) -> str:
    """repo で git を動かし、出力の前後の空白を除いて返す。失敗したら例外。"""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


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


def write_config(path: Path, text: str) -> Path:
    """config.toml を text のとおりに書く（全体の設定だけ。担当は write_agents、定期処理は write_schedules で書く）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _table(columns: tuple[str, ...], rows: str | Iterable[str | dict], key: str) -> str:
    """表の中身。rows が文字列ならそのまま（見出しの行も含める）。並びなら、1つが1行（名前だけなら enabled は true。
    辞書なら書いた列だけ埋め、enabled を省くと true）。"""
    if isinstance(rows, str):
        return rows
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        row = {key: row} if isinstance(row, str) else dict(row)
        enabled = row.get("enabled", True)
        row["enabled"] = ("true" if enabled else "false") if isinstance(enabled, bool) else enabled
        writer.writerow(row)
    return out.getvalue()


def write_agents(home: Path, rows: str | Iterable[str | dict]) -> Path:
    """担当の表（home の agents.csv）を書く。表に無いモジュールはオフ。

    例: `write_agents(home, ["research", {"module": "knowledge", "channels": "knowledge reading"}])`
    """
    from kei_agent.configuration import agents_table

    path = home / agents_table.AGENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_table(agents_table.COLUMNS, rows, "module"), encoding="utf-8")
    return path


def write_schedules(home: Path, rows: str | Iterable[str | dict]) -> Path:
    """定期処理の表（home の schedules.csv）を書く。例: `write_schedules(home, [{"name": "reading", "time": "06:30"}])`"""
    from kei_agent.configuration import schedules_table

    path = home / schedules_table.SCHEDULES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_table(schedules_table.COLUMNS, rows, "name"), encoding="utf-8")
    return path


def use_engine(config, actor: str, provider: str) -> None:
    """担当の AI を変える（agents.csv の engine を書き換えて、起動し直したのと同じ）。"""
    from dataclasses import replace

    config.agent_profiles[actor] = replace(config.agent_profiles[actor], provider=provider)
