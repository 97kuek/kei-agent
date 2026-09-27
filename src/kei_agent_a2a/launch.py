"""モジュールの共通のコマンド（`kei-agent-module <名前> [コマンド]`。docs/extensibility.md）。

コマンドを書かなければ、module.toml の [process] の番地で、modules/<名前>/agent.py の SKILLS と Executor を
A2A のサーバーとして動かす（launchd からは deploy/run-agent.sh <名前>）。コマンドを書けば、そのモジュールの
commands.py の COMMANDS から動かす（例: `kei-agent-module course setup --apply`）。
担当やコマンドを足すのに、src/ と pyproject.toml は触らなくてよい。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from a2a.server.agent_execution import AgentExecutor
from a2a.types import AgentCard
from starlette.applications import Starlette

from kei_agent import modules
from kei_agent.config import load_config
from kei_agent_a2a import server
from kei_agent_a2a.card import RPC_PATH, agent_card


def env_prefix(spec: modules.ModuleSpec) -> str:
    """待ち受けの場所を環境変数で変えるときの頭（KEI_AGENT_<名前>_HOST / _PORT）。"""
    return "KEI_AGENT_" + spec.name.upper().replace("-", "_")


def parts(spec: modules.ModuleSpec) -> tuple[Callable[[str], AgentCard], Callable[[], AgentExecutor]]:
    """その担当の、名刺の作り方と、仕事をこなすところの作り方（agent.py から）。"""
    code = modules.load_agent(spec)
    description = getattr(code, "DESCRIPTION", "") or spec.description

    def build_card(base_url: str) -> AgentCard:
        return agent_card(f"Kei Agent（{spec.label}）", description, base_url, code.SKILLS)

    def build_executor() -> AgentExecutor:
        executor = code.Executor()
        # 制限の表とモデルの一覧を引く名前は、モジュールの名前
        executor.agent = spec.name
        return executor

    return build_card, build_executor


def build_app(spec: modules.ModuleSpec, base_url: str, token: str,
              executor: AgentExecutor | None = None) -> Starlette:
    """担当の A2A アプリ（テスト用。本番は main が同じものを uvicorn で動かす）。"""
    build_card, build_executor = parts(spec)
    if executor is None:
        executor = build_executor()
    executor.agent = spec.name
    return server.build_app(build_card(base_url), executor, RPC_PATH, token)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-module",
                                     description="モジュールの担当プロセスを起動する。コマンドを書けば、そのコマンドを動かす")
    parser.add_argument("name", help="モジュールの名前")
    parser.add_argument("command", nargs="?", help="モジュールのコマンド（例: setup）。書かなければ担当プロセスを起動する")
    args, rest = parser.parse_known_args(argv)
    config = load_config()
    spec = modules.known().get(args.name)
    if spec is None:
        parser.error(f"知らないモジュールです: {args.name}")
    if args.command:
        commands = modules.load_commands(spec)
        if args.command not in commands:
            parser.error(f"モジュール「{spec.name}」に {args.command} というコマンドはありません"
                         f"（あるもの: {', '.join(sorted(commands)) or 'なし'}）")
        commands[args.command](rest)
        return
    if rest:
        parser.error(f"知らない引数です: {' '.join(rest)}")
    if spec.port is None or spec.name not in config.modules:
        parser.error(f"担当プロセスを持つ、オンのモジュールではありません: {args.name}")
    build_card, build_executor = parts(spec)
    server.serve(f"{spec.label}エージェント", build_card, build_executor, RPC_PATH, spec.port, env_prefix(spec))


if __name__ == "__main__":
    main()
