"""仕事エージェントの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。"""

from __future__ import annotations

from kei_agent_a2a.server import agent_entry
from kei_agent_work.card import build_card
from kei_agent_work.executor import WorkExecutor

DEFAULT_PORT = 8789
ENV_PREFIX = "KEI_AGENT_WORK"

build_app, main = agent_entry("仕事エージェント", build_card, WorkExecutor, DEFAULT_PORT, ENV_PREFIX)

if __name__ == "__main__":
    main()
