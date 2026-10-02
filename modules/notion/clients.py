"""ゲートウェイの利用者（client）と、それぞれが届くホーム。

合言葉は、親の合言葉（MAIN_CLIENT_NOTION_GATEWAY_TOKEN）から client ごとに作ったもの
（`kei_agent.storage.notion.gateway_client_token`）だけを受け付ける。親の合言葉そのものは通さない。

| client | 届くホーム | 使える口 |
|---|---|---|
| kei-agent（本体と setup） | 共通ホームと、書いてあるすべてのホーム | MCP と `/notion/v1` |
| モジュール・研究（名前が client） | agents.csv の notion の列に書いた、そのホームだけ | MCP。AI の実行役が Notion に書けるか、コマンドを持たなければ `/notion/v1` も |

コマンドを使える AI の実行役が Notion を読むだけの client には、何でも送れる口（`/notion/v1`）を渡さない（コマンドから叩くと、読むだけの約束を越えてしまう）。
"""

from __future__ import annotations

from hmac import compare_digest

from kei_agent_a2a.api import (
    MAIN_CLIENT,
    NotionConfig,
    ai_runs_shell,
    ai_writes_notion,
    gateway_client_token,
    notion_id,
)


def client_roots(notion: NotionConfig) -> dict[str, frozenset[str]]:
    """client ごとの届くホーム（agents.csv の notion の列）。空の設定は数えない。"""
    homes = notion.client_homes()
    roots = {MAIN_CLIENT: frozenset(notion_id(home) for home in (notion.hub_home, *homes.values()) if home)}
    roots.update({client: frozenset({notion_id(home)}) for client, home in homes.items()})
    return roots


def proxy_clients(notion: NotionConfig) -> frozenset[str]:
    """Notion の API をそのまま中継する口（`/notion/v1`）を使える client。決まった処理（Python）のためのもの。

    その client の AI がコマンドを使えて、しかも Notion を読むだけなら閉じる（コマンドから口を叩くと、読むだけの
    約束を越えて書けてしまう）。書ける AI なら、口を開けても届く範囲（自分のホーム）は変わらない。
    """
    return frozenset({MAIN_CLIENT} | {client for client in notion.client_homes()
                                      if not ai_runs_shell(client) or ai_writes_notion(client)})


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
