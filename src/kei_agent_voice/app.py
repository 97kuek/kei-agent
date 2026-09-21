"""声のレイヤの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。

常駐させる（呼びかけを待ち続けるので、コマンドではなくサービス。docs/voice.md の4節）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from starlette.applications import Starlette

from kei_agent_a2a.server import build_app as _build_app
from kei_agent_a2a.server import serve
from kei_agent_voice.card import RPC_PATH, build_card
from kei_agent_voice.executor import VoiceExecutor
from kei_agent_voice.session import VoiceSession

DEFAULT_PORT = 8790
ENV_PREFIX = "KEI_AGENT_VOICE"


def build_app(base_url: str, token: str = "", executor: VoiceExecutor | None = None,
              listen: bool = False) -> Starlette:
    executor = executor or VoiceExecutor()
    app = _build_app(build_card(base_url), executor, RPC_PATH, token)
    if listen:
        # Starlette 1.6 は on_startup を持たない。lifespan だけ
        app.router.lifespan_context = _ears(executor)
    return app


def _ears(executor: VoiceExecutor):
    """A2A サーバーと同じプロセスで、マイクからも聞く。

    耳が使えない機械でも、口（本体からの知らせ）だけで動く（`VoiceSession._listen` が握りつぶす）。
    """

    @asynccontextmanager
    async def lifespan(app):
        session = VoiceSession(executor.held, mouth=executor.mouth)
        # マイクの開け閉めは、本体が Slack（App Home）から押してくる
        executor.session = session
        # **既定では開けない。** 常に録らない（docs/voice.md の7節）
        task = asyncio.create_task(session.run(listening=False))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            executor.session = None

    return lifespan


def main() -> None:
    # ほかのエージェントと違い、口（A2A）と耳（マイク）を同じプロセスで持つ。
    # 名刺は serve が住所つきで作ってくれるので、それをそのまま使う
    def with_ears(card, executor, path, token):
        app = _build_app(card, executor, path, token)
        app.router.lifespan_context = _ears(executor)
        return app

    serve("声のレイヤ", build_card, VoiceExecutor, RPC_PATH, DEFAULT_PORT, ENV_PREFIX,
          build_app=with_ears)


if __name__ == "__main__":
    main()
