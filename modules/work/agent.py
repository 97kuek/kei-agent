"""仕事の担当プロセス（A2A のサーバー）。起動は共通のコマンド `kei-agent-module work`。

予定・メール・人は、会社のアカウントに付いている Microsoft 365 の連携で読む（`connector.py`）。
自由な質問は、どの担当とも同じ `ask`。返すのは共通の封筒で、見せ方は本体（module.py）が決める。
`list-events` が返すのは件名・時間・場所・リンクまで。朝のまとめで1行ずつ並べる形に合わせている。
"""

from __future__ import annotations

import logging

from kei_agent_a2a.api import ASK, AgentSkill, SkillExecutor, TaskUpdater, provider_of, requested_days

from . import connector
from .skills import LIST_EVENTS

log = logging.getLogger(__name__)

DESCRIPTION = ("会社のアカウントの Outlook・Teams・SharePoint を読む。読み取り専用で、"
               "件名・時間・相手・場所・リンクを、始まる順に返す（1行ずつ並べる形に合わせる）")
SKILLS = [
    AgentSkill(
        id=LIST_EVENTS,
        name="予定を読む",
        description="Outlook の予定を、始まる順に JSON で返す（data.items: subject/start/end/"
                    "location/organizer/url）。既定は7日先まで。本文の JSON の days で変えられる",
        tags=["outlook", "calendar"],
        examples=["今日の予定は？", "明日の会議を教えて", "今週の予定"],
    ),
    AgentSkill(
        id=ASK,
        name="会社のことに答える",
        description="定型に当てはまらない質問に、選択済み provider が答える。Outlook の予定・メール、"
                    "Teams のやりとり、SharePoint の資料、人と空き時間を読んで答える（長い本文は要約する）",
        tags=["outlook", "teams", "sharepoint"],
        examples=["ゆうちょ案件の直近のやりとりは？", "先週のメールで急ぎのものある？", "この資料どこにある？"],
    ),
]
NAMES = tuple(skill.id for skill in SKILLS)
# 予定を読む先の長さの上限（日）
MAX_DAYS = 90


class Executor(SkillExecutor):
    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        if await self.pick(updater, metadata, text, NAMES, LIST_EVENTS) is None:
            return
        days = requested_days(text, connector.DEFAULT_DAYS, MAX_DAYS)
        try:
            events = await connector.events(self.config, self.store, days, provider=provider_of(metadata))
        except connector.WorkCalendarError as e:
            await self.fail(updater, str(e), e.limit_reset_at)
            return
        log.info("予定を %d 件返します（%d 日ぶん）", len(events), days)
        await self.done(updater, f"これから {days} 日の予定は {len(events)} 件", {"days": days, "items": events})
