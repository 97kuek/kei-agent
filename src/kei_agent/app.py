"""Kei Agent の起動: Slack Bolt（Socket Mode）の受け口と、ジョブの定期確認。"""

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

from kei_agent import home, research
from kei_agent.assistant import Assistant
from kei_agent.config import load_config
from kei_agent.jobs import JobManager
from kei_agent.notion_hub import load_hub
from kei_agent.notion_store import load_notion
from kei_agent.schedule import Scheduler
from kei_agent.store import Store

log = logging.getLogger("kei_agent")

REQUIRED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "KEI_AGENT_ALLOWED_USER_ID")
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
    pueue = research.pueue(config)
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
        hub=load_hub(config),
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

    # ボタンと App Home（docs/architecture.md）。Slack は3秒以内の ack を待つので、先に返してから処理する
    def acked(handler):
        async def wrapped(ack, body):
            await ack()
            await guard(handler)(body)
        return wrapped

    app.action(re.compile(r"^kei_agent_domain_(allow|deny)$"))(acked(assistant.on_domain_action))
    app.action(re.compile(r"^kei_agent_home_"))(acked(assistant.on_home_action))
    app.action(re.compile(r"^kei_agent_handoff_(accept|decline)$"))(acked(assistant.on_handoff_action))
    app.action(re.compile(r"^kei_agent_time_(start|stop)$"))(acked(assistant.on_time_action))
    app.action(re.compile(r"^kei_agent_time_(memo|retry)$"))(acked(assistant.on_time_action))

    @app.command("/toggl")
    async def toggl_command(ack, body):
        # ack の文は本人にだけ見える。Toggl や Notion への送信は、on_time_command が裏に回す
        try:
            text = await assistant.on_time_command(body)
        except Exception:
            log.exception("/toggl を処理できませんでした")
            text = "⚠️ 時間記録を切り替えられなかったよ。もう一度試してね。"
        await ack(text)

    @app.view(re.compile(r"^kei_agent_time_(memo|course)_submit$"))
    async def time_card_view(ack, body):
        try:
            errors = await assistant.on_time_view(body)
        except Exception:
            log.exception("時間記録の入力を処理できませんでした")
            errors = {"memo": "保存できませんでした。もう一度試してね"}
        if errors:
            await ack(response_action="errors", errors=errors)
        else:
            await ack()

    @app.view(home.ADD_DOMAIN_CALLBACK)
    async def add_domain(ack, body):
        try:
            errors = await assistant.on_add_domain(body)
        except Exception:
            log.exception("接続先を足せませんでした")
            errors = {"domain": "足せませんでした。Kei Agent のログを見てください"}
        if errors:
            await ack(response_action="errors", errors=errors)
        else:
            await ack()

    job_loop = asyncio.create_task(assistant.job_loop())
    ask_loop = asyncio.create_task(assistant.ask_loop())
    schedule_loop: asyncio.Task | None = None
    questions_loop: asyncio.Task | None = None
    log.info("Kei Agent を起動しました（bot user: %s, research_root: %s, Notion: %s）",
             auth["user_id"], config.research_root, "あり" if assistant.notion else "なし")
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    try:
        # つながらないまま止まると、入れ替えに失敗しても誰も気づけない。時間を切って落ちる
        await asyncio.wait_for(handler.connect_async(), CONNECT_TIMEOUT_SECONDS)
        # 自分を入れ替えたあとの起動なら、その結果をスレッドに知らせる（improve.py）
        await assistant.announce_update()
        # 再起動で途中になった自己改善を「中断」にして、次の着手を止めないようにする
        await assistant.recover_interrupted_fixes()
        # 前の版で動いていて、入れ替えや強制終了で止まった依頼をやり直す
        await assistant.resume_interrupted()
        # Notion の項目がずれていると、Daily や夜間の Task が黙って止まるので、起動時に確かめる
        await assistant.check_notion_schema()
        await assistant.check_hub_schema()
        # つないでいるエージェント（A2A）の名刺を読んで、生きているかを見る
        await assistant.check_agents()
        schedule_loop = asyncio.create_task(Scheduler(config, store, assistant).loop())
        if config.a2a.orchestrator:
            # 声のレイヤからの問い合わせ口（担当を呼べるのは本体だけ。questions.py）
            from kei_agent import questions
            questions_loop = asyncio.create_task(questions.serve(assistant, config.a2a.orchestrator))
        # 取り込みのあと、動いている作業がなくなると立つ。終了すると launchd が新しい版で起動する
        await assistant.restart_requested.wait()
    finally:
        job_loop.cancel()
        ask_loop.cancel()
        for loop in (schedule_loop, questions_loop):
            if loop is not None:
                loop.cancel()
        await handler.close_async()


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
# ネットが切れている間、Slack の接続は数秒ごとに失敗を記録する。同じものはこの秒数に1行だけ残す
REPEAT_SECONDS = 600
# （slack_sdk の socket_mode/aiohttp が、アプリのロガーに書く文言）
REPEATED_MESSAGES = {"slack_bolt.AsyncApp": ("Failed to check the current session", "Failed to connect (error:")}


class RepeatFilter(logging.Filter):
    """同じ種類の失敗が続くときは、最初の1行と、そのあと REPEAT_SECONDS ごとに1行だけ通す（省いた数を添える）。"""

    def __init__(self, prefixes: tuple[str, ...], seconds: float = REPEAT_SECONDS):
        super().__init__()
        self.prefixes = prefixes
        self.seconds = seconds
        self.last: dict[str, float] = {}
        self.dropped: dict[str, int] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        prefix = next((p for p in self.prefixes if message.startswith(p)), None)
        if prefix is None:
            return True
        if record.created - self.last.get(prefix, float("-inf")) < self.seconds:
            self.dropped[prefix] = self.dropped.get(prefix, 0) + 1
            return False
        if self.dropped.get(prefix):
            record.msg, record.args = f"{message}（同じ失敗 {self.dropped[prefix]} 件は省いた）", ()
        self.last[prefix] = record.created
        self.dropped[prefix] = 0
        return True


def setup_logging(env: dict[str, str] | None = None) -> None:
    """KEI_AGENT_LOG_FILE があれば、5MB ごとに回して5世代だけ残す。なければ標準エラーに出す。"""
    env = dict(os.environ) if env is None else env
    handlers: list[logging.Handler] = []
    if env.get("KEI_AGENT_LOG_FILE"):
        path = Path(env["KEI_AGENT_LOG_FILE"]).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"))
    logging.basicConfig(
        level=env.get("KEI_AGENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or None,
        force=True,
    )
    for name, prefixes in REPEATED_MESSAGES.items():
        logger = logging.getLogger(name)
        if not any(isinstance(f, RepeatFilter) for f in logger.filters):
            logger.addFilter(RepeatFilter(prefixes))


def main() -> None:
    setup_logging()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
