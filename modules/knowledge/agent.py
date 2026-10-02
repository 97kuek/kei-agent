"""知識の担当プロセス（A2A のサーバー）。起動は共通のコマンド `kei-agent-module knowledge`。

興味のある技術記事（朝の読みもの）と、研究テーマの論文の新着を集めて絞り、選んだものを要約する。
記事や論文の質問にも答える。外の文を読むので、Slack の鍵・Notion・コマンド・手元のファイルは持たない。
朝の仕事の材料（興味・情報源・テーマの前提）は本体（module.py）が本文の JSON で渡し、結果は本体が Slack と
Notion に出す。自由な質問は、どの担当とも同じ `ask`。返すのは共通の封筒で、見せ方は本体が決める。
"""

from __future__ import annotations

import logging

from kei_agent_a2a.api import ASK, AgentSkill, SkillExecutor, TaskUpdater, body_json, progress, provider_of

from . import digest
from .skills import PAPER_DIGEST, READING_DIGEST

log = logging.getLogger(__name__)

DESCRIPTION = ("興味のある技術記事と、研究テーマの論文の新着を集めて絞り、選んだものを要約する。記事や論文の質問に答える。"
               "Notion・Slack・手元のファイル・コマンドは持たない（材料は本体から受け取る）")
SKILLS = [
    AgentSkill(
        id=READING_DIGEST,
        name="朝の読みもの",
        description="本文の JSON（interests: [{name, keywords}]、sources: [zenn:… / qiita:… / RSS の URL]、"
                    "count）を受け取り、RSS の新着から興味ごとに偏らないよう count 件を選び、本文を読んで"
                    "日本語で要約して返す（data.items: title/url/source/interests/summary/why）",
        tags=["reading"],
    ),
    AgentSkill(
        id=PAPER_DIGEST,
        name="論文の新着",
        description="本文の JSON（theme、keywords、premises、known_ids、count）を受け取り、arXiv の新着から"
                    "前提と関係のあるものだけを最大 count 本選び、要旨から要点と研究との関係を書いて返す"
                    "（data.items: id/title/url/authors/year/venue/summary/relation）",
        tags=["papers", "arxiv"],
    ),
    AgentSkill(
        id=ASK,
        name="記事や論文の質問に答える",
        description="選択済み provider が、記事や論文を Web で読んで答える",
        tags=["reading", "papers"],
        examples=["2番を詳しく", "この記事を要約して https://…", "この論文の手法は何が新しい？"],
    ),
]
NAMES = tuple(skill.id for skill in SKILLS)


class Executor(SkillExecutor):
    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = await self.pick(updater, metadata, text, NAMES, ASK)
        if skill is None:
            return
        try:
            payload = body_json(text)
        except ValueError as e:
            await self.fail(updater, str(e))
            return

        async def say(status: str) -> None:
            await progress(updater, {"activity": status})

        work = digest.reading if skill == READING_DIGEST else digest.papers
        try:
            data = await work(self.config, self.store, payload, provider=provider_of(metadata),
                              progress=say)
        except digest.DigestError as e:
            await self.fail(updater, str(e), e.limit_reset_at)
            return
        except ValueError as e:
            await self.fail(updater, str(e))
            return
        log.info("%s: %d 件を返します（候補 %d）", skill, len(data["items"]), data.get("candidates", 0))
        await self.done(updater, f"{len(data['items'])} 件", data)
