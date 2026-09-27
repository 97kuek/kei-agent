"""音声から、研究・授業・仕事の中身を聞く。担当を呼べるのは本体だけなので、本体の問い合わせ口に頼む。

本体（kei_agent.questions）が provider の選択を確かめ、読むだけで担当に聞き、Slack と同じ出力の確認を
通した答えを返す（窓口の ask_orchestrator）。声のレイヤは担当の住所を持たない。provider 間のフォールバックはしない。
"""

from __future__ import annotations

from dataclasses import dataclass

from kei_agent_a2a.api import Config, OrchestratorError, ask_orchestrator


@dataclass
class Handoff:
    """音声用の読み取り問い合わせ。本体の `ask` に頼み、答えられなければ、そのことを読み上げられる文で返す。"""

    config: Config

    async def ask(self, actor: str, question: str, theme: str = "") -> str:
        try:
            answer = await ask_orchestrator(self.config, actor, question, theme)
        except OrchestratorError as e:
            return f"調べられなかった: {e}"
        return answer or "何も返ってこなかった。"
