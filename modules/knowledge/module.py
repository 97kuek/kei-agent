"""Mac が配信した読みものの控えを扱う互換窓口。

新しい記事・論文の検索、要約、配信、保存は Dot が行う。
既存の控えと保存失敗時の好みを保持し、明示的な保存・解除だけを再試行する。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from kei_agent.api import Core, NotionError, Request

POST = "post"
POST_KEEP_DAYS = 30
# 頭に渡す読みものの項目
READING_FIELDS = ("title", "url", "source", "summary", "why")
class Module:
    def __init__(self, core: Core):
        self.core = core
        self._save_lock = asyncio.Lock()


    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        await self.core.reply(req, "記事や論文の検索・相談・保存は Knowledge の Dot に頼んでください。")

    async def head_materials(self, days: int) -> dict[str, list[dict]]:
        """頭（MCP の reading）に渡す、この days 日に出した読みもの（新しい順）。好みに入れたか、保存したかも添える。"""
        since = (datetime.now().date() - timedelta(days=max(days, 1) - 1)).isoformat()
        posts = sorted((post for post in self.core.records.items(POST) if str(post.get("day") or "") >= since),
                       key=lambda post: (str(post.get("day") or ""), str(post.get("ts") or "")), reverse=True)
        return {"reading": [{"day": post.get("day"), **{key: post["item"].get(key) for key in READING_FIELDS},
                             "liked": bool(post.get("liked_at")), "saved": bool(post.get("page"))}
                            for post in posts if isinstance(post.get("item"), dict)]}

    async def head_action(self, name: str, params: dict) -> dict | None:
        """MCP から URL を指定して保存する。同じ URL の投稿は同じ保存先を使う。"""
        if name != "save_reading":
            return None
        url = params.get("url")
        saved = params.get("saved", True)
        if not isinstance(url, str) or not url.strip():
            raise ValueError("読みものの URL を指定してください")
        if not isinstance(saved, bool):
            raise ValueError("saved は true か false にしてください")
        url = url.strip()
        # Notion を待つ間の再送も、控えを読み直してから扱う
        async with self._save_lock:
            posts = [post for post in self.core.records.items(POST)
                     if isinstance(post.get("item"), dict) and post["item"].get("url") == url]
            if not posts:
                raise ValueError("その URL の読みものは投稿の控えにありません")
            errors = await (self._save(posts) if saved else self._forget(posts))
            return {"url": url, "saved": any(post.get("page") for post in posts),
                    "liked": any(post.get("liked_at") for post in posts), "errors": errors}

    def _remember(self, post: dict, liked_at: float | None, page: str | None) -> None:
        """好みと保存先を控える。好みに入れた記事は、保存の成否によらず残す。"""
        self.core.records.update(POST, f"{post['channel']}:{post['ts']}", liked_at=liked_at, page=page,
                                 keep_days=None if liked_at is not None else POST_KEEP_DAYS)
        post.update(liked_at=liked_at, page=page)

    async def _save(self, posts: list[dict]) -> list[str]:
        """保存先があれば使い直す。保存できなくても好みは控え、失敗の理由を返す。"""
        page = next((post["page"] for post in posts if post.get("page")), None)
        errors = []
        hub = self.core.hub
        if page is None:
            if hub is None or not hub.has_reading_db:
                errors.append("共通ホームに「読みもの」がないので、Notion に保存していません")
            else:
                try:
                    page = await self.core.to_thread(hub.add_reading, posts[0]["item"], posts[0]["day"])
                    if not page:
                        errors.append("Notion の「読みもの」の保存先を確認できませんでした")
                except NotionError as e:
                    errors.append(f"Notion の「読みもの」に保存できませんでした: {e}")
        now = time.time()
        for post in posts:
            self._remember(post, post.get("liked_at") or now, post.get("page") or page)
        return errors

    async def _forget(self, posts: list[dict]) -> list[str]:
        """保存を解除して Notion の行をゴミ箱に入れる。失敗した行は控えを残して再試行できるようにする。"""
        pages = {post["page"] for post in posts if post.get("page")}
        errors = []
        failed = set()
        hub = self.core.hub
        for page in pages:
            if hub is None:
                failed.add(page)
                errors.append("Notion につながっていないので、読みものの保存を解除できません")
                continue
            try:
                await self.core.to_thread(hub.trash_page, page)
            except NotionError as e:
                failed.add(page)
                errors.append(f"Notion の「読みもの」の保存を解除できませんでした: {e}")
        for post in posts:
            if post.get("page") not in failed:
                self._remember(post, None, None)
        return errors
