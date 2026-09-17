"""Ezra の起動: Slack Bolt（Socket Mode）の受け口と、ジョブの定期確認。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ezra.assistant import Assistant
from ezra.config import load_config
from ezra.jobs import JobManager, Pueue
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

    job_loop = asyncio.create_task(assistant.job_loop())
    log.info("Ezra を起動しました（bot user: %s, research_root: %s）", auth["user_id"], config.research_root)
    try:
        await AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async()
    finally:
        job_loop.cancel()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("EZRA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
