"""頼まれた仕事（朝の読みもの、論文の新着、記事や論文の質問）をこなすところ。

朝の仕事の材料（興味・情報源・テーマの前提）は、本体が本文の JSON で渡す。この担当は Notion を読まない。
自由な質問は、どのエージェントとも同じ `ask`（`kei_agent_a2a`）。返すのは共通の封筒で、見せ方は本体が決める。
"""

from __future__ import annotations

import json
import logging

from a2a.server.tasks import TaskUpdater

from kei_agent.config import Config, load_config
from kei_agent.store import Store
from kei_agent_a2a import run
from kei_agent_a2a.executor import SkillExecutor
from kei_agent_knowledge import digest
from kei_agent_knowledge.card import ASK, PAPER_DIGEST, READING_DIGEST

log = logging.getLogger(__name__)

SKILLS = (READING_DIGEST, PAPER_DIGEST, ASK)


class KnowledgeExecutor(SkillExecutor):
    agent = "knowledge"

    def __init__(self, config: Config | None = None, store: Store | None = None):
        self.config = config or load_config()
        self.store = store or Store(self.config.db_path)

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = metadata.get("skill", ASK)
        if skill not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} だけです")
            return
        if skill == ASK:
            await self.answer(updater, text)
            return
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            await self._fail(updater, "材料は本文の JSON で渡してください")
            return

        async def progress(status: str) -> None:
            await run.progress(updater, {"activity": status})

        work = digest.reading if skill == READING_DIGEST else digest.papers
        try:
            data = await work(self.config, self.store, payload, provider=str(metadata.get("provider") or ""),
                              progress=progress)
        except digest.DigestError as e:
            await self._fail(updater, str(e), e.limit_reset_at)
            return
        except ValueError as e:
            await self._fail(updater, str(e))
            return
        log.info("%s: %d 件を返します（候補 %d）", skill, len(data["items"]), data.get("candidates", 0))
        await self._done(updater, f"{len(data['items'])} 件", data)
