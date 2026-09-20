"""仕事エージェントの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。"""

from __future__ import annotations

from starlette.applications import Starlette

from kei_agent_a2a.server import build_app as _build_app
from kei_agent_a2a.server import serve
from kei_agent_work.card import RPC_PATH, build_card
from kei_agent_work.executor import WorkExecutor

DEFAULT_PORT = 8789
ENV_PREFIX = "KEI_AGENT_WORK"


def build_app(base_url: str, token: str = "", executor: WorkExecutor | None = None) -> Starlette:
    return _build_app(build_card(base_url), executor or WorkExecutor(), RPC_PATH, token)


def main() -> None:
    serve("仕事エージェント", build_card, WorkExecutor, RPC_PATH, DEFAULT_PORT, ENV_PREFIX)


if __name__ == "__main__":
    main()
