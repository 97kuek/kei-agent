"""大学エージェントの claude に渡す道具（MCP サーバー）と、その作業場。

- Box … 自分で書いた小さな MCP（`box_mcp.py`）。読み取りだけ。ファイルはローカルに保存しない
- Notion … 公式の MCP（`@notionhq/notion-mcp-server`）。**読み取り専用のコネクト**のトークンを渡す
  （書き込みは Python 側が行う。claude に壊せる鍵を渡さない）

トークンは MCP の設定ファイルに書く。設定ファイルは sandbox から読めない場所に置き、使い終わったら消す
（`kei_agent_a2a.claude.mcp_config`）。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path

from kei_agent import themes
from kei_agent.config import Config
from kei_agent.themes import Workspace
from kei_agent_course import box

log = logging.getLogger(__name__)

AGENT = "course"
# 読み取り専用の Notion コネクト（授業ホームだけに接続したもの）のトークン
NOTION_READ_ENV = "NOTION_COURSE_READ_TOKEN"
NOTION_MCP_PACKAGE = "@notionhq/notion-mcp-server"
NOTION_DOMAINS = ("api.notion.com", "registry.npmjs.org")
# claude 1回の上限時間（分）。Box を読んで答えるだけなので短くする（docs/plan.md の15章）
TIMEOUT_MINUTES = 5


def mcp_servers(config: Config, env: dict[str, str] | None = None) -> dict:
    """claude に差す MCP サーバー。用意できないものは黙って外す（理由はログに書く）。"""
    env = dict(os.environ) if env is None else env
    servers: dict[str, dict] = {}
    command = config.repo_root / ".venv" / "bin" / "kei-agent-box-mcp"
    try:
        app = box.App.from_env(env)
    except box.BoxError as e:
        log.warning("Box の道具を渡せません: %s", e)
    else:
        if not command.exists():
            log.warning("Box の道具が見つかりません: %s（uv sync --group course で入ります）", command)
        else:
            servers["box"] = {
                "command": str(command),
                # 鍵はここだけに置く。claude（の Bash）からは、この設定ファイルも読めない
                "env": {box.CLIENT_ID_ENV: app.client_id, box.CLIENT_SECRET_ENV: app.client_secret},
            }
    token = env.get(NOTION_READ_ENV, "")
    if token and shutil.which("npx"):
        servers["notion"] = {
            "command": "npx",
            "args": ["-y", NOTION_MCP_PACKAGE],
            "env": {"NOTION_TOKEN": token},
        }
    elif not token:
        log.info("%s がないので、Notion の道具は渡しません", NOTION_READ_ENV)
    return servers


def workspace(config: Config, mcp_config: Path | None) -> Workspace:
    """大学エージェントの claude を動かす場所と柵。"""
    name = config.course_channels[0] if config.course_channels else AGENT
    ws = replace(
        themes.resolve(config, name),
        allowed_domains=box.DOMAINS + NOTION_DOMAINS,
        mcp_config=mcp_config,
        system_prompt=config.repo_root / "prompts" / f"{AGENT}.md",
        timeout_minutes=TIMEOUT_MINUTES,
    )
    themes.ensure_workspace(ws)
    return ws
