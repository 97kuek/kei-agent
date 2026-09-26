"""頼まれた仕事（Outlook の予定を読む、会社のことに答える）をこなすところ。

予定・メール・人は、会社のアカウントに付いている Microsoft 365 の連携で読む（`connector.py`）。
自由な質問は、どのエージェントとも同じ `ask`（`kei_agent_a2a`）。

返すのは全エージェント共通の封筒（`kei_agent_a2a/envelope.py`）。見せ方はオーケストレーターが決める。
`list-events` が返すのは件名・時間・場所・リンクまで。朝のまとめで1行ずつ並べる形に合わせている
（docs/architecture.md）。
"""

from __future__ import annotations

import logging

from a2a.server.tasks import TaskUpdater

from kei_agent.config import Config, load_config
from kei_agent.store import Store
from kei_agent_a2a.executor import SkillExecutor, asked_days
from kei_agent_work import connector
from kei_agent_work.skills import ASK, LIST_EVENTS

log = logging.getLogger(__name__)

SKILLS = (LIST_EVENTS, ASK)
# 予定を読む先の長さの上限（日）
MAX_DAYS = 90


class WorkExecutor(SkillExecutor):
    agent = "work"

    def __init__(self, config: Config | None = None, store: Store | None = None):
        self.config = config or load_config()
        self.store = store or Store(self.config.db_path)

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = metadata.get("skill", LIST_EVENTS)
        if skill not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} だけです")
            return
        if skill == ASK:
            await self.answer(updater, text)
            return
        days = asked_days(metadata, connector.DEFAULT_DAYS, MAX_DAYS)
        try:
            provider = str(metadata.get("provider") or "")
            events = await connector.events(self.config, days, store=self.store,
                                            **({"provider": provider} if provider else {}))
        except connector.WorkCalendarError as e:
            await self._fail(updater, str(e), e.limit_reset_at)
            return
        log.info("予定を %d 件返します（%d 日ぶん）", len(events), days)
        await self._done(updater, f"これから {days} 日の予定は {len(events)} 件", {"days": days, "items": events})
