"""Ezra の起動: Slack Bolt（Socket Mode）の受け口と、ジョブの定期確認。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ezra import home
from ezra.assistant import Assistant
from ezra.config import load_config
from ezra.jobs import JobManager, Pueue
from ezra.notion_store import load_notion
from ezra.schedule import Scheduler
from ezra.store import Store

log = logging.getLogger("ezra")

REQUIRED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "EZRA_ALLOWED_USER_ID")
# Slack につながるのを待つ上限。超えたら落ちて、launchd に起動し直してもらう
CONNECT_TIMEOUT_SECONDS = 120


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

    def guard(handler):
        """Slack の出来事の処理で例外が出ても、黙って無反応にならないようにする。"""
        async def wrapped(event):
            try:
                await handler(event)
            except Exception as e:
                log.exception("Slack のイベントの処理に失敗しました")
                await assistant.notify_trouble(f"Slack のイベントを処理できませんでした: {type(e).__name__}: {e}")
        return wrapped

    for name, handler in (
        ("app_mention", assistant.on_mention),
        ("message", assistant.on_message),
        ("member_joined_channel", assistant.on_member_joined),
        ("channel_rename", assistant.on_channel_rename),
        ("group_rename", assistant.on_channel_rename),
        ("reaction_added", assistant.on_reaction_added),
        ("reaction_removed", assistant.on_reaction_removed),
        ("channel_archive", assistant.on_channel_archive),
        ("group_archive", assistant.on_channel_archive),
        ("app_home_opened", assistant.on_home_opened),
    ):
        app.event(name)(guard(handler))

    # ボタンと App Home（docs/plan.md の11章）。Slack は3秒以内の ack を待つので、先に返してから処理する
    def acked(handler):
        async def wrapped(ack, body):
            await ack()
            await guard(handler)(body)
        return wrapped

    app.action(re.compile(r"^ezra_domain_(allow|deny)$"))(acked(assistant.on_domain_action))
    app.action(re.compile(r"^ezra_home_"))(acked(assistant.on_home_action))

    @app.view(home.ADD_DOMAIN_CALLBACK)
    async def add_domain(ack, body):
        try:
            errors = await assistant.on_add_domain(body)
        except Exception:
            log.exception("接続先を足せませんでした")
            errors = {"domain": "足せませんでした。Ezra のログを見てください"}
        if errors:
            await ack(response_action="errors", errors=errors)
        else:
            await ack()

    job_loop = asyncio.create_task(assistant.job_loop())
    ask_loop = asyncio.create_task(assistant.ask_loop())
    schedule_loop = asyncio.create_task(Scheduler(config, store, assistant).loop())
    log.info("Ezra を起動しました（bot user: %s, research_root: %s, Notion: %s）",
             auth["user_id"], config.research_root, "あり" if assistant.notion else "なし")
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    try:
        # つながらないまま止まると、入れ替えに失敗しても誰も気づけない。時間を切って落ちる
        await asyncio.wait_for(handler.connect_async(), CONNECT_TIMEOUT_SECONDS)
        # 自分を入れ替えたあとの起動なら、その結果をスレッドに知らせる（improve.py）
        await assistant.announce_update()
        # 取り込みのあと、動いている作業がなくなると立つ。終了すると launchd が新しい版で起動する
        await assistant.restart_requested.wait()
    finally:
        job_loop.cancel()
        ask_loop.cancel()
        schedule_loop.cancel()
        await handler.close_async()


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
