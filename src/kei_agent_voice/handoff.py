"""音声から、研究・授業・仕事の中身を聞く。担当を呼べるのは本体だけなので、本体の A2A の口に頼む。

本体（kei_agent.questions）が provider の選択を確かめ、読むだけで担当に聞き、Slack と同じ出力の確認を
通した答えを返す。声のレイヤは担当の住所を持たない。provider 間のフォールバックはしない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from kei_agent import a2a, agents
from kei_agent.config import Config

NO_ORCHESTRATOR = "本体の住所が config.toml の [a2a] orchestrator にない。"


@dataclass
class Handoff:
    """音声用の読み取り問い合わせ。本体の `ask` に頼む。"""

    config: Config

    async def ask(self, actor: str, question: str, theme: str = "") -> str:
        url = self.config.a2a.orchestrator
        if not url:
            return NO_ORCHESTRATOR
        # 本体は担当の AI を動かすので、待つ時間はその上限時間を足しておく
        timeout = self.config.run_timeout_minutes * 60 + self.config.a2a.timeout_seconds
        reply = await agents.ask(a2a.Agent(url, self.config.a2a_token, timeout=timeout), agents.ASK,
                                 text=json.dumps({"actor": actor, "question": question, "theme": theme},
                                                 ensure_ascii=False))
        if not reply.ok:
            return f"調べられなかった: {reply.text or '返事が空でした'}"
        return reply.text or "何も返ってこなかった。"
