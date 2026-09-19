"""Ezra の起動: Slack Bolt（Socket Mode）の受け口と、ジョブの定期確認。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ezra.assistant import Assistant
from ezra.config import load_config
from ezra.jobs import JobManager, Pueue
from ezra.notion_store import load_notion
from ezra.schedule import Scheduler
from ezra.store import Store

log = logging.getLogger("ezra")

REQUIRED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "EZRA_ALLOWED_USER_ID")


async def serve() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"環境変数が設定されていません: {', '.join(missing)}（deploy/README.md を参照）")

    config = load_config()
    config.research_root.mkdir(parents=True, exist_ok=True)
    store = Store(config.db_path)
    for name, day in store.mark_interrupted_schedules():
        log.warning("前回の %s（%s）は途中で終わっていました。時間内ならやり直します", name, day)
    pueue = Pueue(config)
    await pueue.ensure_group()

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    auth = await app.client.auth_test()
    assistant = Assistant(
        config=config,
        store=store,
        slack=app.client,
        jobs=JobManager(config, store, pueue),
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        bot_user_id=auth["user_id"],
        notion=load_notion(config),
        team_url=auth.get("url", ""),
        team_id=auth.get("team_id", ""),
    )

    @app.event("app_mention")
    async def handle_mention(event):
        await assistant.on_mention(event)

    @app.event("message")
    async def handle_message(event):
        await assistant.on_message(event)

    @app.event("member_joined_channel")
    async def handle_member_joined(event):
        await assistant.on_member_joined(event)

    @app.event("channel_rename")
    async def handle_channel_rename(event):
        await assistant.on_channel_rename(event)

    @app.event("group_rename")
    async def handle_group_rename(event):
        await assistant.on_channel_rename(event)

    @app.event("reaction_added")
    async def handle_reaction_added(event):
        await assistant.on_reaction_added(event)

    @app.event("reaction_removed")
    async def handle_reaction_removed(event):
        await assistant.on_reaction_removed(event)

    job_loop = asyncio.create_task(assistant.job_loop())
    schedule_loop = asyncio.create_task(Scheduler(config, store, assistant).loop())
    log.info("Ezra を起動しました（bot user: %s, research_root: %s, Notion: %s）",
             auth["user_id"], config.research_root, "あり" if assistant.notion else "なし")
    try:
        await AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async()
    finally:
        job_loop.cancel()
        schedule_loop.cancel()


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5


def setup_logging(env: dict[str, str] | None = None) -> None:
    """EZRA_LOG_FILE があれば、5MB ごとに回して5世代だけ残す。なければ標準エラーに出す。"""
    env = dict(os.environ) if env is None else env
    handlers: list[logging.Handler] = []
    if env.get("EZRA_LOG_FILE"):
        path = Path(env["EZRA_LOG_FILE"]).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"))
    logging.basicConfig(
        level=env.get("EZRA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or None,
        force=True,
    )


def main() -> None:
    setup_logging()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
