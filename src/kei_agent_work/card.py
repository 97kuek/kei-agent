"""仕事エージェントの Agent Card（何ができるかを書いた名刺）。"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

LIST_EVENTS = "list-events"

VERSION = "0.1.0"
RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺。base_url は `http://127.0.0.1:8789` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（仕事）",
        description="会社のアカウントの Outlook を読む（いまは予定だけ）。読み取り専用で、"
                    "個人の Slack には件名・時間・リンクだけを返す",
        version=VERSION,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # いまは数秒で終わる読み取りだけ。claude を持たせるのは、道具が増えてから
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
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
        ],
    )
