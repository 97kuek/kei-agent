"""大学エージェントの起動（A2A サーバー）。

`/.well-known/agent-card.json` で名刺を返し、`/a2a` で JSON-RPC を受ける。
外には出さず、127.0.0.1 でだけ待ち受ける。合言葉（Bearer トークン）が合わない相手は断る。
"""

from __future__ import annotations

import logging
import os
import sys

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from kei_agent_course.card import RPC_PATH, build_card
from kei_agent_course.executor import CourseExecutor

log = logging.getLogger("kei_agent_course")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# 名刺は誰が読んでもよい（秘密は書かない）。仕事を頼むほうには合言葉が要る
OPEN_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json", "/health")


class SharedTokenAuth(BaseHTTPMiddleware):
    """同じ Mac の中だけで使う、合言葉による受け付け。"""

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        if request.url.path in OPEN_PATHS or not self.token:
            return await call_next(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return JSONResponse({"error": "合言葉が違います"}, status_code=401)
        return await call_next(request)


def build_app(base_url: str, token: str = "") -> Starlette:
    card = build_card(base_url)
    handler = DefaultRequestHandler(
        agent_executor=CourseExecutor(), task_store=InMemoryTaskStore(), agent_card=card)
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, RPC_PATH)]
    middleware = [Middleware(SharedTokenAuth, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("KEI_AGENT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("KEI_AGENT_COURSE_HOST", DEFAULT_HOST)
    port = int(os.environ.get("KEI_AGENT_COURSE_PORT", DEFAULT_PORT))
    token = os.environ.get("KEI_AGENT_A2A_TOKEN", "")
    if not token:
        print("KEI_AGENT_A2A_TOKEN がありません（合言葉なしで起動します）", file=sys.stderr)
    log.info("大学エージェントを起動します（http://%s:%s）", host, port)
    uvicorn.run(build_app(f"http://{host}:{port}", token), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
