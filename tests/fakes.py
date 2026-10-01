"""テストで使う偽物。Slack・AI・Notion・ジョブの偽物は kei_agent.testing にある（モジュールを作る人も使う）。
ここには、このリポジトリのテストだけが使うもの（GitHub・依頼のファイル・研究テーマ）を置く。
"""

import json
from pathlib import Path

from kei_agent import ask, themes
from kei_agent.jobs import REQUESTS_DIR
from kei_agent.testing.fakes import (  # noqa: F401  前からの名前で読めるように
    FakeAI,
    FakeHub,
    FakeNotion,
    FakeNotionAPI,
    FakePueue,
    FakeSlack,
    check_notion_body,
)

# 前の名前（AI の偽物は Claude でも Codex でも同じ）
FakeClaude = FakeAI


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
    """置かれている依頼（kei_agent.ask が書いたファイル）を古い順に。本番は claim_asks で拾う。"""
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
    """config.toml を書く。モジュール・チャンネル・AI（modules・[channels]・[agents]）は、同じフォルダの agents.csv に分ける。"""
    import tomllib

    from kei_agent import agents_command, modules
    from kei_agent.configuration import agents_table

    # 利用者のモジュール（同じフォルダの modules/）も、表の行にできるように読んでおく
    modules.register_user_modules(path.parent / "modules")
    try:
        moved = any(key in tomllib.loads(text) for key in agents_table.REPLACED_KEYS)
    except tomllib.TOMLDecodeError:
        moved = False
    if moved:
        text, table = agents_command.split(text)
        (path.parent / agents_table.AGENTS_FILE).write_text(table, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
