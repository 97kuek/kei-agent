"""知識エージェントの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。"""

from __future__ import annotations

from kei_agent_a2a.server import agent_entry
from kei_agent_knowledge.card import build_card
from kei_agent_knowledge.executor import KnowledgeExecutor

DEFAULT_PORT = 8792
ENV_PREFIX = "KEI_AGENT_KNOWLEDGE"

build_app, main = agent_entry("知識エージェント", build_card, KnowledgeExecutor, DEFAULT_PORT, ENV_PREFIX)

if __name__ == "__main__":
    main()
