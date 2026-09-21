"""仕事エージェントの Agent Card（何ができるかを書いた名刺）。"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

LIST_EVENTS = "list-events"
ASK = "ask"

VERSION = "0.1.0"
RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺。base_url は `http://127.0.0.1:8789` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（仕事）",
        description="会社のアカウントの Outlook・SharePoint・Teams を読む。読み取り専用で、"
                    "件名・時間・相手・場所・リンクを、始まる順に返す（1行ずつ並べる形に合わせる）",
        version=VERSION,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # 自由な質問（ask）は claude を動かすので、経過を流しながら返す
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=LIST_EVENTS,
                name="予定を読む",
                description="Outlook の予定を、始まる順に JSON で返す（data.items: subject/start/end/"
                            "location/organizer/url）。既定は7日先まで。metadata の days で変えられる",
                tags=["outlook", "calendar"],
                examples=["今日の予定は？", "明日の会議を教えて", "今週の予定"],
            ),
            AgentSkill(
                id=ASK,
                name="会社のことに答える",
                description="定型に当てはまらない質問に、自分の claude が答える。Outlook のメール、"
                            "SharePoint の資料、Teams のやりとりを読んで答える（長い本文は要約する）",
                tags=["outlook", "sharepoint", "teams"],
                examples=["ゆうちょ案件の直近のやりとりは？", "先週のメールで急ぎのものある？",
                          "この資料どこにある？"],
            ),
        ],
    )
