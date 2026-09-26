"""Agent Card（何ができるかを書いた名刺）の共通の組み立て。

A2A では、相手はまず `/.well-known/agent-card.json` を読んで、何ができるかと、どこに話しかければよいかを知る。
エージェントごとに違うのは、名前・説明・仕事の一覧・受け取る形だけ。
"""

from __future__ import annotations

from collections.abc import Sequence

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from kei_agent import version

# JSON-RPC の窓口（Agent Card の supported_interfaces に載せる）
RPC_PATH = "/a2a"


def agent_card(name: str, description: str, base_url: str, skills: Sequence[AgentSkill], *,
               input_modes: Sequence[str] = ("text/plain",),
               output_modes: Sequence[str] = ("application/json",)) -> AgentCard:
    """base_url は `http://127.0.0.1:8787` のような、外から見える住所。"""
    return AgentCard(
        name=name,
        description=description,
        # 動いている版（起動したときの commit）。本体が古い版の担当を見つけて起動し直すのに使う
        version=version.RUNNING,
        supported_interfaces=[AgentInterface(
            url=base_url.rstrip("/") + RPC_PATH,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        )],
        # claude を動かす仕事があるので、経過を流しながら返す
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=list(input_modes),
        default_output_modes=list(output_modes),
        skills=list(skills),
    )
