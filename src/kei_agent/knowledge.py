"""知識のチャンネル（#40_knowledge）と、朝の読みもの・論文の新着を、知識エージェントに取り次ぐ。

知識エージェントは外の記事を読む担当なので、Slack の鍵も Notion も持たない（docs/architecture.md の「知識」）。
興味と情報源（共通ホームの「収集」ページ）、テーマの検索キーワードと前提（テーマの CLAUDE.md）は本体が読んで渡し、
返ってきた記事・論文を本体が Slack に出す。論文は研究ホームの先行研究 DB にも書く。
読みものは1記事 = 1投稿。👍 を付けた記事は共通ホームの「読みもの」に入れ、次からの選び方の参考として渡す。

Assistant に混ぜて使う。self.agents、self.post などは Assistant のもの。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from kei_agent import agents, settings
from kei_agent.notion import NotionError
from kei_agent.request import Request
from kei_agent.slack_text import escape
from kei_agent_knowledge.skills import ASK as ASK
from kei_agent_knowledge.skills import PAPER_DIGEST as PAPER_DIGEST
from kei_agent_knowledge.skills import READING_DIGEST as READING_DIGEST

log = logging.getLogger(__name__)

# config.toml の [a2a.agents] で書いたエージェントの名前
AGENT = "knowledge"
# 朝に出す本数（読みものは全体で、論文はテーマごと）
READING_COUNT = 5
PAPERS_PER_THEME = 5
# 読みものの 👍（肌の色の違いも同じ）。付けた記事は Notion の「読みもの」に入れ、本体が 📝 を付けて知らせる
LIKE_REACTIONS = frozenset({"+1", "thumbsup"})
SAVED_REACTION = "memo"
# 次の読みものを選ぶときに参考として渡す 👍（この日数のうち、新しいものからこの件数）
LIKED_DAYS = 60
LIKED_EXAMPLES = 20
LIKE_HINT = "気になった記事に 👍 を付けると、Notion の「読みもの」に入って、次から似た記事を選びやすくなるよ。質問はそれぞれのスレッドで。"
# チャンネルに招かれたときの案内
CAN_DO = ("このチャンネルでできること。\n"
          f"• 毎朝… 共通ホームの「収集」ページの興味と情報源から、読みものを{READING_COUNT}件出す\n"
          "• 「2番を詳しく」… 朝の一覧のスレッドで、その記事を読んで答える\n"
          "• URL つきの質問… 記事や論文を読んで答える")


def _url(value: object) -> str:
    """Slack のリンク。自分で <> で囲んで、どこまでが URL かを示す（すぐ後の「（Zenn）」まで URL にされないように）。

    中に `|` があるとそこから先が表示名に、`<` `>` があるとそこで区切られてしまうので外す。
    """
    url = str(value or "").split("|")[0].replace("<", "").replace(">", "").strip()
    return f"<{url}>" if url else ""


def reading_post_text(item: dict, number: int, total: int, hint: bool = False) -> str:
    """朝の読みもの1件（1記事 = 1投稿なので、👍 とスレッドが記事ごとになる）。hint は最後の1件にだけ付ける。"""
    lines = [f"📰 {number}/{total} *{escape(str(item.get('title') or ''))}*",
             escape(str(item.get("summary") or "")),
             f"→ {escape(str(item.get('why') or ''))}",
             f"{_url(item.get('url'))}（{escape(str(item.get('source') or ''))}）"]
    if hint:
        lines += ["", LIKE_HINT]
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


def _base_reaction(name: object) -> str:
    """肌の色の違い（`+1::skin-tone-2`）を外した名前。"""
    return str(name or "").split("::")[0]


class KnowledgeChannel:
    async def reading_reaction(self, event: dict, added: bool) -> bool:
        """朝の読みものへの依頼者の 👍（付けた・外した）。読みものの 👍 でなければ何もせず False。"""
        item = event.get("item") or {}
        if (_base_reaction(event.get("reaction")) not in LIKE_REACTIONS or item.get("type") != "message"
                or not self.is_allowed(event.get("user"))):
            return False
        row = self.store.reading_post(str(item.get("channel") or ""), str(item.get("ts") or ""))
        if row is None:
            return False
        if added and row["liked_at"] is None:
            await self._save_reading(row)
        elif not added and row["liked_at"] is not None:
            await self._forget_reading(row)
        return True

    async def _save_reading(self, row) -> None:
        """👍 した記事を「読みもの」に入れる。Notion に入らなくても、次からの参考には使う。"""
        page_id = None
        if self.hub is None or not self.hub.has_reading_db:
            if not self.store.noticed("reading-db-missing"):
                self.store.record_notice("reading-db-missing")
                await self.notify_trouble("共通ホームに「読みもの」がないので、👍 した記事を Notion に入れていません"
                                          "（kei-agent-hub-setup --apply で作れます）")
        else:
            try:
                page_id = await asyncio.to_thread(self.hub.add_reading, json.loads(row["item"]), row["day"])
            except NotionError as e:
                await self.notify_trouble(f"👍 した記事を Notion の「読みもの」に入れられませんでした: {e}")
        self.store.set_reading_like(row["channel"], row["ts"], time.time(), page_id)
        if page_id:
            await self._react(self.slack.reactions_add, row["channel"], row["ts"], SAVED_REACTION)

    async def _forget_reading(self, row) -> None:
        """👍 を外したら、Notion の行もゴミ箱に入れる（Notion の画面から戻せる）。"""
        page_id = row["notion_page_id"]
        if page_id and self.hub is not None:
            try:
                await asyncio.to_thread(self.hub.trash_page, page_id)
            except NotionError as e:
                await self.notify_trouble(f"👍 を外した記事を Notion の「読みもの」から消せませんでした: {e}")
        self.store.set_reading_like(row["channel"], row["ts"], None, None)
        if page_id:
            await self._react(self.slack.reactions_remove, row["channel"], row["ts"], SAVED_REACTION)

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
