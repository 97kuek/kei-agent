"""モジュールの共通のコマンド（`kei-agent-module <名前> [コマンド]`。docs/extensibility.md）。

コマンドを書かなければ、module.toml の [process] の番地で、modules/<名前>/agent.py の SKILLS と Executor を
A2A のサーバーとして動かす（launchd からは deploy/run-agent.sh <名前>）。コマンドを書けば、そのモジュールの
commands.py の COMMANDS から動かす（例: `kei-agent-module course setup --apply`）。
担当やコマンドを足すのに、src/ と pyproject.toml は触らなくてよい。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress

from a2a.server.agent_execution import AgentExecutor
from a2a.types import AgentCard
from starlette.applications import Starlette

from kei_agent import modules
from kei_agent.config import load_config
from kei_agent_a2a import server
from kei_agent_a2a.card import RPC_PATH, agent_card

log = logging.getLogger(__name__)


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


def app_builder(spec: modules.ModuleSpec) -> Callable[[AgentCard, AgentExecutor, str, str], Starlette]:
    """A2A のアプリの作り方。agent.py に background(executor) があれば、担当と同じプロセスで動かし続ける。

    background は起動のときに始め、止めるときに止める。落ちたらログに残す（A2A の口は動かし続ける）。
    """
    background = getattr(modules.load_agent(spec), "background", None)

    def build(card: AgentCard, executor: AgentExecutor, rpc_path: str, token: str) -> Starlette:
        app = server.build_app(card, executor, rpc_path, token)
        if background is not None:
            app.router.lifespan_context = _lifespan(spec, background, executor)
        return app

    return build


def _lifespan(spec: modules.ModuleSpec, background, executor: AgentExecutor):
    @asynccontextmanager
    async def lifespan(_app):
        def stopped(done: asyncio.Task) -> None:
            if not done.cancelled() and done.exception() is not None:
                log.error("モジュール「%s」の background が止まりました", spec.name, exc_info=done.exception())

        task = asyncio.create_task(background(executor))
        task.add_done_callback(stopped)
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return lifespan


def build_app(spec: modules.ModuleSpec, base_url: str, token: str,
              executor: AgentExecutor | None = None) -> Starlette:
    """担当の A2A アプリ（テスト用。本番は main が同じものを uvicorn で動かす）。"""
    build_card, build_executor = parts(spec)
    if executor is None:
        executor = build_executor()
    executor.agent = spec.name
    return app_builder(spec)(build_card(base_url), executor, RPC_PATH, token)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-module",
                                     description="モジュールの担当プロセスを起動する。コマンドを書けば、そのコマンドを動かす")
    parser.add_argument("name", help="モジュールの名前")
    parser.add_argument("command", nargs="?", help="モジュールのコマンド（例: setup）。書かなければ担当プロセスを起動する")
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) >= 2 and not argv[0].startswith("-") and not argv[1].startswith("-"):
        # コマンドのあとの引数（--help も）は、そのコマンドにそのまま渡す
        name, command, rest = argv[0], argv[1], argv[2:]
    else:
        args = parser.parse_args(argv)
        name, command, rest = args.name, args.command, []
    config = load_config()
    spec = modules.known().get(name)
    if spec is None:
        parser.error(f"知らないモジュールです: {name}")
    if command:
        commands = modules.load_commands(spec)
        if command not in commands:
            parser.error(f"モジュール「{spec.name}」に {command} というコマンドはありません"
                         f"（あるもの: {', '.join(sorted(commands)) or 'なし'}）")
        result = commands[command](rest)
        if isinstance(result, int) and result:
            raise SystemExit(result)
        return
    if spec.port is None or spec.name not in config.modules:
        parser.error(f"担当プロセスを持つ、オンのモジュールではありません: {name}")
    if spec.service:
        # A2A ではない常駐のプロセス（Notion のゲートウェイなど）。止められるまで戻らない。番地は担当と同じく
        # 環境変数 KEI_AGENT_<名前>_PORT で変えられる
        port = int(os.environ.get(f"{env_prefix(spec)}_PORT", spec.port))
        raise SystemExit(modules.load_service(spec).serve(config, port) or 0)
    build_card, build_executor = parts(spec)
    server.serve(f"{spec.label}エージェント", build_card, build_executor, RPC_PATH, spec.port, env_prefix(spec),
                 build_app=app_builder(spec))


if __name__ == "__main__":
    main()
