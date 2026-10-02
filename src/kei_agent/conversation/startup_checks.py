"""起動のときと定期的な確かめ: 担当の名刺と版、Notion の DB の形。

Assistant に混ぜて使う。self.slack、self.store、self.config などは Assistant のもの。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from kei_agent.execution import updates
from kei_agent.framework import version

log = logging.getLogger(__name__)

# 名刺（エージェントのスキル）を読み直す間隔。入れ替えても、これだけたてば新しいスキルを使える
SKILLS_TTL_SECONDS = 600
# 古い版の担当を起動し直してから、名刺を読み直すまでの秒数
STALE_RECHECK_SECONDS = 20


class StartupChecks:
    async def check_agents(self) -> dict[str, list[str]]:
        """つないでいるエージェントの名刺を読んで、生きているか、何ができるか、本体と同じ版かを見る。

        古い版のまま動いている担当は起動し直す（手作業のデプロイで担当だけ起動し直し忘れると、古いコードが
        新しい設定を読めずに止まる）。
        """
        skills: dict[str, list[str]] = {}
        stale: list[str] = []
        for name, agent in self.agents.items():
            try:
                card = await agent.card()
            except Exception as e:
                await self.notify_trouble(f"{name} のエージェントにつながりません（{agent.base_url}）: "
                                          f"{type(e).__name__}: {e}")
                continue
            self._remember_skills(name, card)
            skills[name] = [s["id"] for s in self.agent_skills[name] if s.get("id")]
            log.info("%s のエージェントにつながりました（%s）: %s", name, card.get("name", "?"),
                     "、".join(skills[name]) or "できることなし")
            if version.differs(str(card.get("version") or "")):
                log.warning("%s の担当が古い版のまま動いています（本体 %s、担当 %s）", name, version.RUNNING,
                            card.get("version"))
                stale.append(name)
        if stale:
            self.spawn(self._restart_stale_agents(stale))
        return skills

    async def _restart_stale_agents(self, names: list[str]) -> None:
        """古い版の担当を起動し直し、少し待って確かめる。それでも古ければ知らせる。"""
        for name in names:
            await asyncio.to_thread(updates.restart_service, name)
        await asyncio.sleep(STALE_RECHECK_SECONDS)
        for name in names:
            try:
                theirs = str((await self.agents[name].card()).get("version") or "")
            except Exception:
                theirs = ""
            if theirs != version.RUNNING:
                await self.notify_trouble(f"{name} の担当が古い版のまま動いています。"
                                          "deploy/restart-all.sh で起動し直してください")

    def _remember_skills(self, name: str, card: dict) -> None:
        self.agent_skills[name] = list(card.get("skills") or [])
        self.agent_skills_read_at[name] = time.time()

    async def skills_of(self, name: str) -> list[dict]:
        """そのエージェントのスキル（名刺から）。古くなっていたら読み直す。"""
        fresh = time.time() - self.agent_skills_read_at.get(name, 0) < SKILLS_TTL_SECONDS
        if not fresh and name in self.agents:
            with suppress(Exception):
                self._remember_skills(name, await self.agents[name].card())
        return self.agent_skills.get(name, [])

    async def check_notion_schema(self) -> list[str]:
        """Notion の項目のずれを起動時に見て、あれば知らせる。黙って定期処理が止まるのを防ぐ。"""
        if self.notion is None:
            return []
        try:
            problems = await asyncio.to_thread(self.notion.schema_problems)
        except Exception:
            log.exception("Notion の設定を確かめられませんでした")
            await self.notify_trouble("Notion の設定を確かめられませんでした")
            return []
        if problems:
            await self.notify_trouble(
                "研究 Notion の設定が Kei Agent の使う形とずれています。夜間の Task などが止まります。\n"
                + "\n".join(f"• {p}" for p in problems))
        return problems

    async def check_hub_schema(self) -> list[str]:
        """共通ホームの問題は知らせるが、他の agent や研究 Notion の起動を止めない。"""
        if self.hub is None:
            await self.notify_trouble(
                "共通 Notion ホームを利用できません。親ページの共有と hub state を確認してください。"
                "Daily とレトプラは Slack にだけ出し、時間の記録は Notion への送信を保留します")
            return ["共通 Notion ホームを利用できません"]
        try:
            problems = await asyncio.to_thread(self.hub.schema_problems)
        except Exception:
            log.exception("共通 Notion ホームの設定を確かめられませんでした")
            self.hub = None
            await self.notify_trouble("共通 Notion ホームの設定を確認できません。Daily とレトプラは Notion に保存できません")
            return ["共通 Notion ホームの確認に失敗しました"]
        if problems:
            self.hub = None
            await self.notify_trouble("共通 Notion ホームの項目を確認してください:\n"
                                      + "\n".join(f"• {p}" for p in problems))
        elif not self.hub.has_time_db:
            await self.notify_trouble(
                "共通 Notion ホームに「時間記録」がまだありません。kei-agent-hub-setup --apply で作って再起動してください"
                "（それまで時間は Toggl にだけ送り、Notion への送信は保留します）")
        return problems

