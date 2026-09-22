from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kei_agent.config import Config

TOKEN_ENV = "KEI_AGENT_NOTION_GATEWAY_TOKEN"


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    token: str
    notion_token: str
    state_path: Path


def load_gateway_config(config: Config, env: Mapping[str, str]) -> GatewayConfig:
    token = env.get(TOKEN_ENV, "").strip()
    notion_token = env.get("NOTION_TOKEN", "").strip()
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} がありません")
    if not notion_token:
        raise RuntimeError("NOTION_TOKEN がありません")
    return GatewayConfig("127.0.0.1", 8791, token, notion_token, config.state_dir / "notion.json")
