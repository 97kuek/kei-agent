"""大学エージェントの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。"""

from __future__ import annotations

from starlette.applications import Starlette

from kei_agent_a2a.server import build_app as _build_app
from kei_agent_a2a.server import serve
from kei_agent_course.card import RPC_PATH, build_card
from kei_agent_course.executor import CourseExecutor

DEFAULT_PORT = 8787
ENV_PREFIX = "KEI_AGENT_COURSE"


def build_app(base_url: str, token: str) -> Starlette:
    return _build_app(build_card(base_url), CourseExecutor(), RPC_PATH, token)


def main() -> None:
    serve("大学エージェント", build_card, CourseExecutor, RPC_PATH, DEFAULT_PORT, ENV_PREFIX)


if __name__ == "__main__":
    main()
