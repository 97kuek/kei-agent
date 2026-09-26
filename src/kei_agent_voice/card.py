"""声のレイヤの Agent Card（何ができるかを書いた名刺）。"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from kei_agent import version

NOTIFY = "notify"

RPC_PATH = "/a2a"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺。base_url は `http://127.0.0.1:8790` のような、外から見える住所。"""
    return AgentCard(
        name="Kei Agent（声）",
        description="机の上で喋る口。本体から出来事を受け取り、言い方と顔は自分で決める。"
                    "Stack-chan がいればそちらで、いなければ Mac のスピーカーで鳴らす",
        version=version.RUNNING,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # 本体は投げっぱなしにする（机の上のロボットに Slack を待たせない。docs/architecture.md の「声のレイヤ」）
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=NOTIFY,
                name="知らせる",
                description="出来事を受け取って、喋る・顔を変える。渡すのは何が起きたかだけ（kind と、"
                            "その中身）。文と顔と首は声のレイヤが組み立てる。"
                            "kind: schedule（朝のまとめ。喋らず手元に置く）、due（締切）、"
                            "working（依頼を受けた。顔だけ）、done（終わった）、failed（止まった）、"
                            "limited（契約の上限）、awaiting（返事待ち）",
                tags=["voice"],
                examples=['{"kind": "done", "theme": "amr-query"}'],
            ),
        ],
    )
