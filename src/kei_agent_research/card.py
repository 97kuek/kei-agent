"""研究エージェントの Agent Card（何ができるかを書いた名刺）。"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

# 仕事の名前。オーケストレーターはこの id を指定して頼む
RUN_CLAUDE = "run-claude"
# 長い処理（ジョブ）。pueue を持つのはこちら側で、行き先の管理（どのスレッドのジョブか）は本体
SUBMIT_JOB = "submit-job"
LIST_JOBS = "list-jobs"
CANCEL_JOB = "cancel-job"
FORGET_JOB = "forget-job"

VERSION = "0.1.0"
RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺を作る。base_url は `http://127.0.0.1:8788` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（研究）",
        description="研究テーマのディレクトリで claude を sandbox の中で動かし、長い処理を pueue のジョブにする。"
                    "会話の続け方と Slack への見せ方、ジョブの行き先の管理はオーケストレーターが持つ",
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
            AgentSkill(
                id=SUBMIT_JOB,
                name="ジョブを投入する",
                description='JSON（cwd・command・label）を受け取り、pueue の待ち行列に入れて task_id を返す。'
                            "cwd は研究テーマのディレクトリの中だけ",
                tags=["pueue"],
                examples=['{"cwd": "~/research/amr-query", "command": "uv run x.py", "label": "kei-agent-3"}'],
            ),
            AgentSkill(
                id=LIST_JOBS,
                name="ジョブの状態",
                description="待ち行列にあるジョブの状態をまとめて返す（data.tasks に pueue の中身）",
                tags=["pueue"],
                examples=["{}"],
            ),
            AgentSkill(
                id=CANCEL_JOB,
                name="ジョブを止める",
                description='JSON（task_id）で、走っているジョブを止める',
                tags=["pueue"],
                examples=['{"task_id": 12}'],
            ),
            AgentSkill(
                id=FORGET_JOB,
                name="ジョブを片づける",
                description='JSON（task_id）で、終わったジョブを待ち行列から消す',
                tags=["pueue"],
                examples=['{"task_id": 12}'],
            ),
        ],
    )
