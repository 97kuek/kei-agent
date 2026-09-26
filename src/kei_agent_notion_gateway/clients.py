"""ゲートウェイの利用者（client）と、それぞれが届くホーム。

合言葉は、親の合言葉（KEI_AGENT_NOTION_GATEWAY_TOKEN）から client ごとに作ったもの
（`kei_agent.notion.gateway_client_token`）だけを受け付ける。親の合言葉そのものは通さない。

| client | 届くホーム | 使える口 |
|---|---|---|
| kei-agent | 共通・研究・授業 | MCP と `/notion/v1` |
| course | 授業 | MCP と `/notion/v1` |
| research | 研究 | MCP だけ（研究の LLM は Bash を持つので、何でも送れる口は渡さない） |
"""

from __future__ import annotations

from hmac import compare_digest

from kei_agent.config import NotionConfig, notion_id
from kei_agent.notion import GATEWAY_CLIENTS, gateway_client_token

KEI_AGENT, RESEARCH, COURSE = GATEWAY_CLIENTS
# Notion の API をそのまま中継する口（`/notion/v1`）を使える client。決まった処理（Python）だけ
PROXY_CLIENTS = frozenset({KEI_AGENT, COURSE})


def client_roots(notion: NotionConfig) -> dict[str, frozenset[str]]:
    """client ごとの届くホーム（config.toml の `[notion]`）。空の設定は数えない。"""
    homes = {
        KEI_AGENT: (notion.hub_home, notion.research_home, notion.course_home),
        RESEARCH: (notion.research_home,),
        COURSE: (notion.course_home,),
    }
    return {client: frozenset(notion_id(home) for home in ids if home) for client, ids in homes.items()}


class Tokens:
    """Authorization ヘッダーから client を決める。"""

    def __init__(self, master: str):
        self._tokens = {client: gateway_client_token(master, client).encode() for client in GATEWAY_CLIENTS}

    def client(self, authorization: str) -> str | None:
        scheme, _, given = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            return None
        found = None
        # どれに当たっても最後まで比べる（当たった位置で時間が変わらないように）
        for client, token in self._tokens.items():
            if compare_digest(given.strip().encode(), token):
                found = client
        return found
