"""Notion の夜間 Task。予定の時刻と延期は本体が扱う。"""

import asyncio
import logging
from contextlib import suppress

from kei_agent.api import Core, NotionError, Request, clean_text, parse_slack_permalink, summarize

log = logging.getLogger(__name__)


class Module:
    def __init__(self, core: Core):
        self.core = core

    def schedule_provider(self, name: str) -> str:
        return self.core.workspace_provider

    async def run_schedule(self, name: str, day: str) -> dict:
        notion = self.core.notion
        if notion is None:
            return {"status": "no_notion"}
        try:
            tasks = await asyncio.to_thread(notion.tonight_tasks, self.core.night_max_tasks)
        except NotionError as e:
            await self.core.notify_trouble(f"夜間の Task を Notion から読めなかったので、今夜は実行しません: {e}")
            return {"status": "error", "error": str(e)}
        ids = await self.core.channel_ids()
        done = []
        for task in tasks:
            try:
                done.append(await self._run_night_task(task, ids))
            except Exception as e:
                # 「実行中」のまま残ると二度と実行されないので、確認待ちに戻して知らせる
                log.exception("夜間の Task「%s」が止まりました", task.title)
                reason = f"途中で止まりました: {type(e).__name__}: {e}"
                await self.core.notify_trouble(f"夜間の Task「{task.title}」が{reason}")
                with suppress(NotionError):
                    await asyncio.to_thread(notion.update_task, task.id, "確認待ち", reason)
                done.append({"title": task.title, "status": "error", "reason": reason, "url": task.url})
        try:
            remaining = await asyncio.to_thread(notion.count_tonight_tasks)
        except NotionError:
            remaining = None
        return {"status": "done", "tasks": done, "remaining": remaining}

    async def _run_night_task(self, task, ids: dict[str, str]) -> dict:
        notion = self.core.notion
        info = {"title": task.title, "url": task.url, "theme": ", ".join(task.theme_names)}
        theme = task.theme_names[0] if task.theme_names else None
        channel_name = theme
        if theme is None or channel_name not in ids:
            reason = "テーマを設定してください" if theme is None else f"テーマのチャンネル #{theme} に Kei Agent がいません"
            await asyncio.to_thread(notion.update_task, task.id, "確認待ち", reason)
            return {**info, "status": "確認待ち", "reason": reason}

        await asyncio.to_thread(notion.update_task, task.id, "実行中")
        body = await asyncio.to_thread(notion.page_markdown, task.id)
        channel = ids[channel_name]
        source = parse_slack_permalink(task.slack_url)
        message: dict = {}
        # 元のメッセージがテーマのチャンネルにあるときだけ、そのスレッドで続ける
        if source and source[0] == channel:
            message = await self.core.fetch_message(*source) or {}
        if message:
            message_ts = source[1]
            thread_ts = message.get("thread_ts") or message_ts
        else:
            thread_ts = await self.core.post(channel, f"🌙 Task: {task.title}")
            await asyncio.to_thread(notion.update_task, task.id, None, None,
                                    await self.core.permalink(channel, thread_ts))

        text = (
            "[🌙 夜間の Task] 依頼者は寝ているので、その場で聞き返せません。"
            "判断が必要なところまで進めたら、最後の行を「❓ 確認:」で始めて止めてください。\n\n"
            f"タイトル: {task.title}\n優先度: {task.priority or '-'} / 期日: {task.due or '-'}\n"
            f"Notion: {task.url}\n\n## 本文\n\n{body or '（なし）'}\n"
        )
        if message:
            text += f"\n## 元の Slack のメッセージ\n\n{clean_text(message.get('text', ''))}\n"
        req = Request(channel, channel_name, thread_ts, None, text, trigger="night")
        result = await self.core.dispatch(req)

        if result.failed:
            status = "確認待ち"
            summary = "エラーで止まりました"
        else:
            status = "確認待ち" if result.awaiting else "完了"
            summary = summarize(result.text)
        await asyncio.to_thread(notion.update_task, task.id, status, summary)
        return {**info, "status": status, "summary": summary}

