from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kei_agent.config import Config
from kei_agent.notion import GATEWAY_TOKEN_ENV
from kei_agent_notion_gateway.clients import client_roots

TOKEN_ENV = GATEWAY_TOKEN_ENV


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    # 親の合言葉。client ごとの合言葉を作るのにだけ使い、これ自体では通さない
    master: str
    notion_token: str
    # client → 届くホームのページ ID（config.toml の [notion]）
    roots: dict[str, frozenset[str]]


def load_gateway_config(config: Config, env: Mapping[str, str]) -> GatewayConfig:
    master = env.get(TOKEN_ENV, "").strip()
    notion_token = env.get("NOTION_TOKEN", "").strip()
    if not master:
        raise RuntimeError(f"{TOKEN_ENV} がありません")
    if not notion_token:
        raise RuntimeError("NOTION_TOKEN がありません")
    roots = client_roots(config.notion)
    if not any(roots.values()):
        raise RuntimeError("config.toml の [notion] にホームのページ ID がありません")
    return GatewayConfig("127.0.0.1", 8791, master, notion_token, roots)
