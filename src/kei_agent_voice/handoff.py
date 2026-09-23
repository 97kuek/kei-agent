"""音声から、選択済み provider の担当 agent へ内容確認を委譲する。"""

from __future__ import annotations

from dataclasses import dataclass

from kei_agent import agents, research, themes
from kei_agent.config import Config
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve_selected
from kei_agent.store import Store

_CASES = {
    "research": UseCase.RESEARCH_EXTRACT,
    "course": UseCase.COURSE_EXPLAIN,
    "work": UseCase.WORK_SINGLE_SOURCE,
}


@dataclass
class Handoff:
    """音声用の読み取り問い合わせ。provider 間のフォールバックはしない。"""

    config: Config
    store: Store

    async def ask(self, actor: str, question: str, theme: str = "") -> str:
        if actor not in _CASES:
            return "研究、授業、仕事のどれを調べるか分からなかった。"
        if not question.strip():
            return "何を調べるか分からなかった。"
        try:
            resolve_selected(self.config, self.store, actor, _CASES[actor])
        except ModelPolicyError as e:
            return str(e)
        remote = agents.build(self.config).get(actor)
        if remote is None:
            return f"{actor} 担当が起動していない。"
        if actor == "research":
            if not theme.strip():
                return "どの研究テーマを調べるかも教えて。"
            try:
                ws = themes.resolve(self.config, theme.strip().lstrip("#"))
            except ValueError:
                return "その研究テーマは使えない名前だった。"
            result = await research.run(remote, ws, question.strip(), None, "", "", _CASES[actor], read_only=True)
            if result.is_error:
                return "調べられなかった: " + "; ".join(result.errors[:1])
            return result.text or "何も返ってこなかった。"
        reply = await agents.ask(remote, "ask", text=question.strip())
        return reply.text if reply.ok and reply.text else f"調べられなかった: {reply.text or '返事が空でした'}"
