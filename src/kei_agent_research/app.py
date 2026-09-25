"""研究エージェントの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。"""

from __future__ import annotations

from kei_agent_a2a.server import agent_entry
from kei_agent_research.card import build_card
from kei_agent_research.executor import ResearchExecutor

DEFAULT_PORT = 8788
ENV_PREFIX = "KEI_AGENT_RESEARCH"

build_app, main = agent_entry("研究エージェント", build_card, ResearchExecutor, DEFAULT_PORT, ENV_PREFIX)

if __name__ == "__main__":
    main()
