"""研究エージェントの Agent Card（何ができるかを書いた名刺）。"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

# 仕事の名前。オーケストレーターはこの id を指定して頼む
RUN_CLAUDE = "run-claude"

VERSION = "0.1.0"
RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺を作る。base_url は `http://127.0.0.1:8788` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（研究）",
        description="研究テーマのディレクトリで claude を sandbox の中で動かす。会話の続け方と Slack への"
                    "見せ方はオーケストレーターが持ち、ここは1回分の実行と、その経過だけを返す",
        version=VERSION,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # 経過を流しながら返す（claude は数分〜数十分かかるので、終わるまで黙っていられない）
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=RUN_CLAUDE,
                name="claude を1回動かす",
                description="JSON（channel_name・prompt・session_id・allowed_domains）を受け取り、"
                            "そのテーマのディレクトリで claude を1回動かして、結果を JSON で返す。"
                            "途中の経過（使った道具と、返答の断片）はタスクの状態に流す",
                tags=["claude", "sandbox"],
                examples=['{"channel_name": "amr-query", "prompt": "図を作って"}'],
            ),
        ],
    )
