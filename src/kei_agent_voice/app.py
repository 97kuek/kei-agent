"""声のレイヤの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。

常駐させる（呼びかけを待ち続けるので、コマンドではなくサービス。docs/voice.md の4節）。
"""

from __future__ import annotations

from starlette.applications import Starlette

from kei_agent_a2a.server import build_app as _build_app
from kei_agent_a2a.server import serve
from kei_agent_voice.card import RPC_PATH, build_card
from kei_agent_voice.executor import VoiceExecutor

DEFAULT_PORT = 8790
ENV_PREFIX = "KEI_AGENT_VOICE"


def build_app(base_url: str, token: str = "", executor: VoiceExecutor | None = None) -> Starlette:
    return _build_app(build_card(base_url), executor or VoiceExecutor(), RPC_PATH, token)


def main() -> None:
    serve("声のレイヤ", build_card, VoiceExecutor, RPC_PATH, DEFAULT_PORT, ENV_PREFIX)


if __name__ == "__main__":
    main()
