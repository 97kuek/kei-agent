"""研究エージェントの Agent Card（何ができるかを書いた名刺）。形は `kei_agent_a2a.card`。"""

from __future__ import annotations

from a2a.types import AgentCard, AgentSkill

from kei_agent_a2a.card import agent_card
from kei_agent_a2a.executor import ASK

# 仕事の名前。オーケストレーターはこの id を指定して頼む
# 長い処理（ジョブ）。pueue を持つのはこちら側で、行き先の管理（どのスレッドのジョブか）は本体
SUBMIT_JOB = "submit-job"
LIST_JOBS = "list-jobs"
CANCEL_JOB = "cancel-job"
FORGET_JOB = "forget-job"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺を作る。base_url は `http://127.0.0.1:8788` のような、外から見える住所。"""
    return agent_card(
        "Kei Agent（研究）",
        "研究テーマのディレクトリで Claude または Codex を sandbox の中で動かし、長い処理を pueue のジョブにする。"
        "会話の続け方と Slack への見せ方、ジョブの行き先の管理はオーケストレーターが持つ",
        base_url,
        input_modes=("application/json",),
        skills=[
            AgentSkill(
                id=ASK,
                name="研究用 provider を1回動かす",
                description="JSON（channel_name・prompt・session_id・allowed_domains）を受け取り、"
                            "そのテーマのディレクトリで選択済み provider を1回動かして、結果を JSON で返す。"
                            "途中の状態は固定の利用者向け文だけをタスクの状態に流す",
                tags=["claude", "codex", "sandbox"],
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
