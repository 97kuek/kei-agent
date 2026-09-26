"""声のレイヤからの問い合わせを受ける、本体の A2A の口（docs/architecture.md の「声のレイヤ」）。

担当のエージェント（研究・大学・仕事）を呼べるのは本体だけ。声からの「研究・授業・仕事の中身を教えて」は
ここで受けて、読むだけで担当に頼み、Slack に出すときと同じ出力の確認を通した文だけを返す
（Assistant.answer_question）。住所は `config.toml` の `[a2a] orchestrator`。
"""

from __future__ import annotations

import json
import logging
import urllib.parse

from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCard, AgentSkill

from kei_agent_a2a.card import RPC_PATH, agent_card
from kei_agent_a2a.executor import ASK, SkillExecutor
from kei_agent_a2a.server import build_app

log = logging.getLogger(__name__)

NO_QUESTION = '依頼は JSON（{"actor": "research|course|work", "question": "…", "theme": "…"}）で渡してください'


def build_card(base_url: str) -> AgentCard:
    return agent_card(
        "Kei Agent（本体）",
        "声のレイヤからの問い合わせを受け、研究・大学・仕事の担当に読むだけで聞いて、確かめた答えを返す",
        base_url,
        input_modes=("application/json",),
        skills=[AgentSkill(
            id=ASK,
            name="担当に聞く",
            description="JSON（actor: research|course|work、question、研究なら theme）を受け取り、"
                        "その担当に読むだけで聞いて、Slack に出すときと同じ確認を通した答えを返す",
            tags=["voice"],
            examples=['{"actor": "course", "question": "今日の授業は？"}'],
        )],
    )


class QuestionExecutor(SkillExecutor):
    """問い合わせを Assistant に渡す。担当への頼み方（provider の確認、読むだけ）は Assistant が持つ。"""

    def __init__(self, assistant):
        self.assistant = assistant

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        if metadata.get("skill", ASK) != ASK:
            await self._fail(updater, f"できるのは {ASK} だけです")
            return
        try:
            asked = json.loads(text)
        except ValueError:
            asked = None
        if not isinstance(asked, dict):
            await self._fail(updater, NO_QUESTION)
            return
        answer = await self.assistant.answer_question(
            str(asked.get("actor") or ""), str(asked.get("question") or ""), str(asked.get("theme") or ""))
        await self._done(updater, answer)


async def serve(assistant, url: str) -> None:
    """本体のプロセスの中で、A2A の口を開く（止められるまで待つ）。開けなくても本体は止めない。"""
    import uvicorn

    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        await assistant.notify_trouble(f"本体の A2A の口は 127.0.0.1 のポートで指定してください: {url}")
        return
    app = build_app(build_card(url), QuestionExecutor(assistant), RPC_PATH, assistant.config.a2a_token)
    server = uvicorn.Server(uvicorn.Config(app, host=parsed.hostname, port=parsed.port, log_level="warning"))
    log.info("本体の A2A の口を開きます（%s）", url)
    try:
        await server.serve()
    except SystemExit:
        # ポートが使われているなど。uvicorn は SystemExit で知らせる（本体の Slack の口は動かし続ける）
        log.error("本体の A2A の口を開けませんでした（%s）", url)
        await assistant.notify_trouble(f"声のレイヤからの問い合わせ口（{url}）を開けませんでした")
