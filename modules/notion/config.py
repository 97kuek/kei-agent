from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kei_agent_a2a.api import GATEWAY_TOKEN_ENV, Config

from .clients import client_roots, proxy_clients

TOKEN_ENV = GATEWAY_TOKEN_ENV


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    # 親の合言葉。client ごとの合言葉を作るのにだけ使い、これ自体では通さない
    master: str
    notion_token: str
    # client → 届くホームのページ ID（agents.csv の notion の列）
    roots: dict[str, frozenset[str]]
    # Notion の API をそのまま中継する口（/notion/v1）を使える client
    proxy: frozenset[str] = frozenset()


def load_gateway_config(config: Config, env: Mapping[str, str], port: int = 8791) -> GatewayConfig:
    master = env.get(TOKEN_ENV, "").strip()
    notion_token = env.get("NOTION_TOKEN", "").strip()
    if not master:
        raise RuntimeError(f"{TOKEN_ENV} がありません")
    if not notion_token:
        raise RuntimeError("NOTION_TOKEN がありません")
    roots = client_roots(config.notion)
    if not any(roots.values()):
        raise RuntimeError("agents.csv の notion の列にホームのページ ID がありません")
    return GatewayConfig("127.0.0.1", port, master, notion_token, roots, proxy_clients(config.notion))
