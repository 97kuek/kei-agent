"""ゲートウェイの利用者（client）と、それぞれが届くホーム。

合言葉は、親の合言葉（KEI_AGENT_NOTION_GATEWAY_TOKEN）から client ごとに作ったもの
（`kei_agent.notion.gateway_client_token`）だけを受け付ける。親の合言葉そのものは通さない。

| client | 届くホーム | 使える口 |
|---|---|---|
| kei-agent（本体と setup） | 共通ホームと、書いてあるすべてのホーム | MCP と `/notion/v1` |
| モジュール・研究（名前が client） | config.toml の [notion] に書いた、そのホームだけ | MCP。シェルを持つ AI の実行役がいなければ `/notion/v1` も |

研究（AI が Bash を持つ）のように、シェルを使える AI の実行役がいる client には、何でも送れる口（`/notion/v1`）を渡さない。
"""

from __future__ import annotations

from hmac import compare_digest

from kei_agent import agent_policy
from kei_agent.config import NotionConfig, notion_id
from kei_agent.notion import KEI_AGENT, gateway_client_token


def client_roots(notion: NotionConfig) -> dict[str, frozenset[str]]:
    """client ごとの届くホーム（config.toml の [notion]）。空の設定は数えない。"""
    homes = notion.client_homes()
    roots = {KEI_AGENT: frozenset(notion_id(home) for home in (notion.hub_home, *homes.values()) if home)}
    roots.update({client: frozenset({notion_id(home)}) for client, home in homes.items()})
    return roots


def _ai_has_shell(client: str) -> bool:
    """その client の名前の AI の実行役が、シェル（コマンド）を使えるか。実行役がいなければ False。"""
    try:
        return agent_policy.policy_of(client).shell
    except ValueError:
        return False


def proxy_clients(notion: NotionConfig) -> frozenset[str]:
    """Notion の API をそのまま中継する口（`/notion/v1`）を使える client。決まった処理（Python）のためのもの。"""
    return frozenset({KEI_AGENT} | {client for client in notion.client_homes() if not _ai_has_shell(client)})


class Tokens:
    """Authorization ヘッダーから client を決める。"""

    def __init__(self, master: str, clients):
        self._tokens = {client: gateway_client_token(master, client).encode() for client in clients}

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
