"""このエージェントの Agent Card（何ができるかを書いた名刺）。

A2A では、相手はまず `/.well-known/agent-card.json` を読んで、何ができるかと、どこに話しかければよいかを知る。
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

# 仕事の名前。オーケストレーターはこの id を指定して頼む
SYNC_ASSIGNMENTS = "sync-assignments"
LIST_DUE = "list-due"
TIME_REPORT = "time-report"

VERSION = "0.1.0"
# JSON-RPC の窓口（Agent Card の supported_interfaces に載せる）
RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺を作る。base_url は `http://127.0.0.1:8787` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（大学）",
        description="Moodle の課題、Notion の授業と課題、Toggl の実績を扱う。Slack には出さず、頼まれた結果を返す",
        version=VERSION,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # 数秒で終わる仕事しかないので、流しながら返す機能は持たない（docs/plan.md の16章）
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id=SYNC_ASSIGNMENTS,
                name="課題を取り込む",
                description="Moodle から履修中の科目と課題を読み、Notion の授業・課題データベースに反映する。"
                            "新しく増えた課題と、締切が変わった課題を返す",
                tags=["moodle", "notion"],
                examples=["課題を取り込んで", "Moodle を見てきて"],
            ),
            AgentSkill(
                id=LIST_DUE,
                name="締切の近い課題",
                description="締切が近い順に課題を返す。既定では7日先まで",
                tags=["notion"],
                examples=["今週の締切は？", "明日までの課題を教えて"],
            ),
            AgentSkill(
                id=TIME_REPORT,
                name="実績時間の集計",
                description="Toggl の記録を科目ごと・課題ごとに集計して返す（読むだけ）",
                tags=["toggl"],
                examples=["今週、どの授業に何時間使った？"],
            ),
        ],
    )
