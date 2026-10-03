"""声のレイヤからの問い合わせを受ける、本体の A2A の口（docs/architecture.md の「声」）。

担当のエージェント（研究・仕事）を呼べるのは本体だけ。声からの「研究・仕事の中身を教えて」は
ここで受けて、読むだけで担当に頼み、Slack に出すときと同じ出力の確認を通した文だけを返す
（Assistant.answer_question）。住所は `config.toml` の `[a2a] orchestrator`。
"""

from __future__ import annotations

import json
import logging

from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCard, AgentSkill

from kei_agent.conversation.loopback import serve_loopback
from kei_agent_a2a.card import RPC_PATH, agent_card
from kei_agent_a2a.executor import ASK, SkillExecutor
from kei_agent_a2a.server import build_app

log = logging.getLogger(__name__)

NO_QUESTION = '依頼は JSON（{"actor": "research|work", "question": "…", "theme": "…"}）で渡してください'


def build_card(base_url: str) -> AgentCard:
    return agent_card(
        "Kei Agent（本体）",
        "声のレイヤからの問い合わせを受け、研究・仕事の担当に読むだけで聞いて、確かめた答えを返す",
        base_url,
        input_modes=("application/json",),
        skills=[AgentSkill(
            id=ASK,
            name="担当に聞く",
            description="JSON（actor: research|work、question、研究なら theme）を受け取り、"
                        "その担当に読むだけで聞いて、Slack に出すときと同じ確認を通した答えを返す",
            tags=["voice"],
            examples=['{"actor": "work", "question": "今日の会議は？"}'],
        )],
    )


class QuestionExecutor(SkillExecutor):
    """問い合わせを Assistant に渡す。担当への頼み方（provider の確認、読むだけ）は Assistant が持つ。"""

    def __init__(self, assistant):
        self.assistant = assistant

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        if metadata.get("skill", ASK) != ASK:
            await self.fail(updater, f"できるのは {ASK} だけです")
            return
        try:
            asked = json.loads(text)
        except ValueError:
            asked = None
        if not isinstance(asked, dict):
            await self.fail(updater, NO_QUESTION)
            return
        answer = await self.assistant.answer_question(
            str(asked.get("actor") or ""), str(asked.get("question") or ""), str(asked.get("theme") or ""))
        await self.done(updater, answer)


async def serve(assistant, url: str) -> None:
    """本体のプロセスの中で、A2A の口を開く（止められるまで待つ）。開けなくても本体は止めない。"""
    await serve_loopback(assistant, url, "本体の A2A の口（声のレイヤからの問い合わせ口）", lambda host: build_app(
        build_card(url), QuestionExecutor(assistant), RPC_PATH, assistant.config.a2a_token))
