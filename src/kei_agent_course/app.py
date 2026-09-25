"""大学エージェントの起動（A2A サーバー）。土台は `kei_agent_a2a.server`。"""

from __future__ import annotations

from kei_agent_a2a.server import agent_entry
from kei_agent_course.card import build_card
from kei_agent_course.executor import CourseExecutor

DEFAULT_PORT = 8787
ENV_PREFIX = "KEI_AGENT_COURSE"

build_app, main = agent_entry("大学エージェント", build_card, CourseExecutor, DEFAULT_PORT, ENV_PREFIX)

if __name__ == "__main__":
    main()
