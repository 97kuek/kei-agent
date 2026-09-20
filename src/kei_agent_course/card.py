"""このエージェントの Agent Card（何ができるかを書いた名刺）。

A2A では、相手はまず `/.well-known/agent-card.json` を読んで、何ができるかと、どこに話しかければよいかを知る。
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

# 仕事の名前。オーケストレーターはこの id を指定して頼む
SYNC_ASSIGNMENTS = "sync-assignments"
LIST_DUE = "list-due"
TIME_REPORT = "time-report"
# 定型に当てはまらない質問の窓口（どのエージェントでも同じ名前。docs/agents.md）
ASK = "ask"

VERSION = "0.1.0"
# JSON-RPC の窓口（Agent Card の supported_interfaces に載せる）
RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺を作る。base_url は `http://127.0.0.1:8787` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（大学）",
        description="Moodle の課題、Notion の授業と課題、Toggl の実績、Box の学部要項と過去問を扱う。"
                    "自由な質問には、自分の claude が Box と Notion を読んで答える",
        version=VERSION,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # 自由な質問（ask）は claude を動かすので、経過を流しながら返す
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
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
                description="締切が近い順に JSON で返す（items: id/at/course/title/url）。"
                            "既定では2週間先まで。metadata の days で変えられる",
                tags=["moodle"],
                examples=["今週の締切は？", "明日までの課題を教えて"],
            ),
            AgentSkill(
                id=ASK,
                name="授業のことに答える",
                description="定型に当てはまらない質問に、自分の claude が答える。Box の学部要項・過去問と、"
                            "Notion の授業・課題を読んで、根拠（ファイル名と URL）を付けて返す",
                tags=["box", "notion", "claude"],
                examples=["情報セキュリティBの過去問ある？", "卒業に必要な単位数は？", "この課題の出し方どうだった？"],
            ),
            AgentSkill(
                id=TIME_REPORT,
                name="実績時間の集計",
                description="Toggl の記録を科目ごと・課題ごとに集計して返す（読むだけ）。"
                            "既定は直近7日。metadata の days で変えられる",
                tags=["toggl"],
                examples=["今週、どの授業に何時間使った？", "先週の実績を見せて"],
            ),
        ],
    )
