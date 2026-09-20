"""A2A サーバーの立ち上げ（大学・研究のエージェントで共通）。

`/.well-known/agent-card.json` で名刺を返し、`/a2a` で JSON-RPC を受ける。
外には出さず、127.0.0.1 でだけ待ち受ける。合言葉（Bearer トークン）が合わない相手は断る。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

DEFAULT_HOST = "127.0.0.1"
# 名刺は誰が読んでもよい（秘密は書かない）。仕事を頼むほうには合言葉が要る
OPEN_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json", "/health")
TOKEN_ENV = "KEI_AGENT_A2A_TOKEN"


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


def build_app(card: AgentCard, executor: AgentExecutor, rpc_path: str, token: str = "") -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card)
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, rpc_path)]
    middleware = [Middleware(SharedTokenAuth, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware)


def serve(name: str, build_card: Callable[[str], AgentCard], build_executor: Callable[[], AgentExecutor],
          rpc_path: str, default_port: int, env_prefix: str) -> None:
    """launchd から起動されたときの入口。"""
    import uvicorn

    logging.basicConfig(level=os.environ.get("KEI_AGENT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get(f"{env_prefix}_HOST", DEFAULT_HOST)
    port = int(os.environ.get(f"{env_prefix}_PORT", default_port))
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"{TOKEN_ENV} がありません（合言葉なしで起動します）", file=sys.stderr)
    base_url = f"http://{host}:{port}"
    logging.getLogger(env_prefix.lower()).info("%sを起動します（%s）", name, base_url)
    app = build_app(build_card(base_url), build_executor(), rpc_path, token)
    uvicorn.run(app, host=host, port=port, log_level="warning")
