"""知識のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 朝の読みもの（reading）… 共通ホームの「収集」ページの興味と情報源と、最近 👍 した記事を担当に渡し、
  選ばれた記事を1記事 = 1投稿で出す（👍 とスレッドが記事ごとになる）
- 先行研究の新着（literature）… 研究テーマの検索キーワードと前提（CLAUDE.md）を担当に渡し、選ばれた論文を
  研究ホームの先行研究 DB と、テーマのチャンネルに出す。そのスレッドの続きは、この担当が答える
- 👍 … 読みものに依頼者が 👍 を付けたら、共通ホームの「読みもの」に入れて 📝 を付ける。外したらゴミ箱へ
- 知識のチャンネルと、論文の新着のスレッドの質問 … 担当に聞いて答える

担当（同じフォルダの agent.py）は外の記事を読むので、Slack の鍵も Notion も持たない。材料は本体が読んで渡し、
返ってきた記事・論文を本体が出す（docs/agents/knowledge-agent.md）。
"""

from __future__ import annotations

import logging
import time

from kei_agent.api import Core, NotionError, Request, day_label, escape

from .skills import PAPER_DIGEST, READING_DIGEST

log = logging.getLogger(__name__)

# 朝に出す本数（読みものは全体で、論文はテーマごと）
READING_COUNT = 5
PAPERS_PER_THEME = 5
# 読みものの投稿の控え（記録の種類）。👍 していないものは、この日数で忘れる
POST = "post"
POST_KEEP_DAYS = 30
# 読みものの 👍（肌の色の違いも同じ）。付けた記事は Notion の「読みもの」に入れ、📝 を付けて知らせる
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


class Module:
    default_question = "今日の読みものについて教えて"

    def __init__(self, core: Core):
        self.core = core

    def welcome(self) -> str:
        return CAN_DO

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """知識のチャンネルと、論文の新着のスレッドの依頼。どれも自由な質問として、担当に聞く。"""
        await self.core.converse(req)

    async def run_schedule(self, name: str, day: str) -> dict:
        return await (self.literature(day) if name == "literature" else self.reading(day))

    # 先行研究の新着

    async def literature(self, day: str) -> dict:
        notion = self.core.notion
        if notion is None:
            return {"status": "no_notion"}
        ids = await self.core.channel_ids()
        try:
            known = set(await self.core.to_thread(notion.paper_ids))
        except (NotionError, KeyError) as e:
            log.warning("先行研究 DB を読めません: %s", e)
            return {"status": "error", "error": f"先行研究 DB を読めません: {e}"}
        results: dict[str, dict] = {}
        for theme in self.core.themes():
            if theme.name not in ids:
                continue  # アーカイブしたテーマや、Kei Agent のいないテーマは見張らない
            if not theme.keywords:
                results[theme.name] = {"status": "no_keywords"}
                continue
            reply = await self.core.ask_agent(PAPER_DIGEST, {
                "theme": theme.name, "keywords": list(theme.keywords), "premises": theme.premises,
                "known_ids": sorted(known), "count": PAPERS_PER_THEME})
            if not reply.ok:
                results[theme.name] = {"status": "error"}
                continue
            items = reply.data.get("items") or []
            if not items:
                results[theme.name] = {"status": "no_new"}
                continue
            try:
                await self.core.to_thread(notion.add_papers, theme.name, items, "毎朝の新着")
            except (NotionError, KeyError) as e:
                log.warning("先行研究 DB に書けません（%s）: %s", theme.name, e)
                results[theme.name] = {"status": "error", "error": f"先行研究 DB に書けません: {e}"}
                continue
            known |= {str(item.get("id")) for item in items}
            channel = ids[theme.name]
            thread_ts = await self.core.post(channel, papers_text(items, day_label(day)))
            if thread_ts:
                # このスレッドの続きは、この担当が答える（ほかのスレッドは研究の担当）
                self.core.claim_thread(channel, thread_ts, theme.name)
            results[theme.name] = {"status": "posted", "count": len(items), "thread_ts": thread_ts}
        failed = any(result.get("status") == "error" for result in results.values())
        return {"status": "error" if failed else "done", "themes": results}

    # 朝の読みもの

    async def reading(self, day: str) -> dict:
        channels = self.core.channels("knowledge")
        name = channels[0] if channels else ""
        channel = (await self.core.channel_ids()).get(name) if name else None
        if channel is None:
            return {"status": "no_channel"}
        hub = self.core.hub
        if hub is None:
            return {"status": "no_hub"}
        try:
            interests, sources = await self.core.to_thread(hub.collect_settings)
        except NotionError as e:
            log.warning("「収集」ページを読めません: %s", e)
            return {"status": "error", "error": f"「収集」ページを読めません: {e}"}
        if not interests or not sources:
            return {"status": "no_settings"}
        reply = await self.core.ask_agent(READING_DIGEST, {
            "interests": interests, "sources": sources, "count": READING_COUNT, "liked": self.liked()})
        if not reply.ok:
            return {"status": "error"}
        items = reply.data.get("items") or []
        failed = [str(source) for source in reply.data.get("failed_sources") or []]
        if not items:
            return {"status": "no_new", "failed_sources": failed}
        for number, item in enumerate(items, 1):
            ts = await self.core.post(channel, reading_post_text(item, number, len(items), hint=number == len(items)))
            if ts:
                # スレッドの質問はこの担当へ（元の投稿も渡る）。👍 はこの控えで記事を知る
                self.core.watch_thread(channel, ts, name)
                self.core.records.put(POST, f"{channel}:{ts}", {
                    "channel": channel, "ts": ts, "day": day, "item": item, "liked_at": None, "page": None},
                    keep_days=POST_KEEP_DAYS)
        return {"status": "posted", "count": len(items), "channel": channel, "failed_sources": failed}

    def liked(self) -> list[dict]:
        """最近 👍 した記事（新しい順）。次の読みものを選ぶときの参考に、題名・出どころ・興味だけを渡す。"""
        since = time.time() - LIKED_DAYS * 86400
        posts = sorted((post for post in self.core.records.items(POST) if (post.get("liked_at") or 0) >= since),
                       key=lambda post: post["liked_at"], reverse=True)
        return [{key: post["item"].get(key) for key in ("title", "source", "interests")}
                for post in posts[:LIKED_EXAMPLES]]

    # 👍

    async def on_reaction(self, event: dict, added: bool) -> bool:
        """朝の読みものへの依頼者の 👍（付けた・外した）。読みものの 👍 でなければ何もせず False。"""
        item = event.get("item") or {}
        if (_base_reaction(event.get("reaction")) not in LIKE_REACTIONS or item.get("type") != "message"
                or not self.core.is_owner(event.get("user"))):
            return False
        post = self.core.records.get(POST, f"{item.get('channel')}:{item.get('ts')}")
        if post is None:
            return False
        if added and post["liked_at"] is None:
            await self._save(post)
        elif not added and post["liked_at"] is not None:
            await self._forget(post)
        return True

    async def _save(self, post: dict) -> None:
        """👍 した記事を「読みもの」に入れる。Notion に入らなくても、次からの参考には使う（控えは消さずに残す）。"""
        page = None
        hub = self.core.hub
        if hub is None or not hub.has_reading_db:
            if self.core.notice_once("reading-db-missing"):
                await self.core.notify_trouble("共通ホームに「読みもの」がないので、👍 した記事を Notion に入れていません"
                                               "（kei-agent-hub-setup --apply で作れます）")
        else:
            try:
                page = await self.core.to_thread(hub.add_reading, post["item"], post["day"])
            except NotionError as e:
                await self.core.notify_trouble(f"👍 した記事を Notion の「読みもの」に入れられませんでした: {e}")
        self.core.records.update(POST, f"{post['channel']}:{post['ts']}", liked_at=time.time(), page=page,
                                 keep_days=None)
        if page:
            await self.core.react(post["channel"], post["ts"], SAVED_REACTION)

    async def _forget(self, post: dict) -> None:
        """👍 を外したら、Notion の行もゴミ箱に入れる（Notion の画面から戻せる）。"""
        page = post.get("page")
        hub = self.core.hub
        if page and hub is not None:
            try:
                await self.core.to_thread(hub.trash_page, page)
            except NotionError as e:
                await self.core.notify_trouble(f"👍 を外した記事を Notion の「読みもの」から消せませんでした: {e}")
        self.core.records.update(POST, f"{post['channel']}:{post['ts']}", liked_at=None, page=None,
                                 keep_days=POST_KEEP_DAYS)
        if page:
            await self.core.react(post["channel"], post["ts"], SAVED_REACTION, remove=True)
