"""知識のチャンネル（#40_knowledge）と、朝の読みもの・論文の新着を、知識エージェントに取り次ぐ。

知識エージェントは外の記事を読む担当なので、Slack の鍵も Notion も持たない（docs/architecture.md の「知識」）。
興味と情報源（共通ホームの「収集」ページ）、テーマの検索キーワードと前提（テーマの CLAUDE.md）は本体が読んで渡し、
返ってきた記事・論文を本体が Slack に出す。論文は研究ホームの先行研究 DB にも書く。

Assistant に混ぜて使う。self.agents、self.post などは Assistant のもの。
"""

from __future__ import annotations

import json
import logging

from kei_agent import agents, settings
from kei_agent.request import Request
from kei_agent.slack_text import escape

log = logging.getLogger(__name__)

# 知識エージェントの仕事の名前（src/kei_agent_knowledge/card.py と同じもの）
READING_DIGEST = "reading-digest"
PAPER_DIGEST = "paper-digest"
ASK = "ask"
# config.toml の [a2a.agents] で書いたエージェントの名前
AGENT = "knowledge"
# 朝に出す本数（読みものは全体で、論文はテーマごと）
READING_COUNT = 5
PAPERS_PER_THEME = 5
# チャンネルに招かれたときの案内
CAN_DO = ("このチャンネルでできること。\n"
          f"• 毎朝… 共通ホームの「収集」ページの興味と情報源から、読みものを{READING_COUNT}件出す\n"
          "• 「2番を詳しく」… 朝の一覧のスレッドで、その記事を読んで答える\n"
          "• URL つきの質問… 記事や論文を読んで答える")


def _url(value: object) -> str:
    # リンクの中に `|` が入ると、そこから先が表示名になってしまう
    return str(value or "").split("|")[0]


def reading_text(items: list[dict], day: str) -> str:
    """朝の読みもの（1通、番号つき）。質問は、このスレッドで番号を言えばよい。"""
    lines = [f"📰 今日の読みもの {day}"]
    for number, item in enumerate(items, 1):
        lines += ["", f"{number}. *{escape(str(item.get('title') or ''))}*",
                  escape(str(item.get("summary") or "")),
                  f"→ {escape(str(item.get('why') or ''))}",
                  f"{_url(item.get('url'))}（{escape(str(item.get('source') or ''))}）"]
    return "\n".join(lines)


def papers_text(items: list[dict], day: str) -> str:
    """テーマごとの論文の新着（1通、番号つき）。先行研究 DB には「未読」で入っている。"""
    lines = [f"📚 先行研究の新着 {day}"]
    for number, item in enumerate(items, 1):
        lines += ["", f"{number}. *{escape(str(item.get('title') or ''))}*",
                  escape(str(item.get("summary") or "")),
                  f"この研究との関係: {escape(str(item.get('relation') or ''))}",
                  _url(item.get("url"))]
    lines += ["", "先行研究 DB に「未読」で入れたよ。詳しくは、このスレッドで聞いてね。"]
    return "\n".join(lines)


class KnowledgeChannel:
    async def knowledge(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """知識のチャンネルと、論文の新着のスレッドの依頼。どれも自由な質問として、知識エージェントに聞く。"""
        await self.converse_with_agent(req, AGENT)

    async def ask_knowledge(self, skill: str, payload: dict) -> agents.Reply:
        """知識エージェントに朝の仕事を頼む。材料（興味・テーマの前提など）は本文で渡す（ログに残さない）。"""
        agent = self.agents.get(AGENT)
        if agent is None:
            await self.notify_trouble("知識エージェントの住所が config.toml の [a2a.agents] にありません")
            return agents.Reply.broken("知識エージェントの住所がないよ")
        provider = settings.selected_provider(self.config, self.store, AGENT)

        async def keep_alive(_status: str) -> None:
            # AI を動かす仕事なので、経過を流しながら受け取る（途中で切られないように）
            return None

        reply = await agents.ask(agent, skill, params={"provider": provider}, on_progress=keep_alive,
                                 text=json.dumps(payload, ensure_ascii=False))
        if not reply.ok:
            await self.notify_trouble(f"知識エージェント（{agent.base_url}）の {skill} が返した理由: {reply.text[:300]}")
        await self.note_limit(reply, AGENT, provider)
        return reply
