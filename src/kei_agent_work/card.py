"""仕事エージェントの Agent Card（何ができるかを書いた名刺）。形は `kei_agent_a2a.card`。"""

from __future__ import annotations

from a2a.types import AgentCard, AgentSkill

from kei_agent_a2a.card import agent_card

LIST_EVENTS = "list-events"
ASK = "ask"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺。base_url は `http://127.0.0.1:8789` のような、外から見える住所。"""
    return agent_card(
        "Kei Agent（仕事）",
        "会社のアカウントの Outlook・SharePoint・Teams を読む。読み取り専用で、"
        "件名・時間・相手・場所・リンクを、始まる順に返す（1行ずつ並べる形に合わせる）",
        base_url,
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
