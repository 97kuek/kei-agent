"""Kei Agent の起動: MCP サーバー・定期処理・ジョブを動かす。

Slack の受信と投稿は Dot が担当する。本体とモジュールの通知は Outbox に保存し、Dot が MCP の notices で取得する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from kei_agent.configuration.config import load_config
from kei_agent.conversation.assistant import Assistant
from kei_agent.conversation.service import create_assistant
from kei_agent.execution import jobs
from kei_agent.scheduling.schedule import Scheduler
from kei_agent.storage.store import Store

log = logging.getLogger("kei_agent")


async def serve() -> None:
    if not os.environ.get("KEI_AGENT_ALLOWED_USER_ID"):
        sys.exit("環境変数が設定されていません: KEI_AGENT_ALLOWED_USER_ID（deploy/README.md を参照）")
    config = load_config()
    config.research_root.mkdir(parents=True, exist_ok=True)
    store = Store(config.db_path)
    for name, day in store.mark_interrupted_schedules():
        log.warning("前回の %s（%s）は途中で終わっていました。時間内ならやり直します", name, day)
    pueue = jobs.queue(config)
    await pueue.ensure_group()
    assistant = create_assistant(config, store, pueue)
    log.info("Kei Agent を起動しました（research_root: %s, Notion: %s）。通知は MCP の notices で渡します",
             config.research_root, "あり" if assistant.notion else "なし")
    if not config.hands_url:
        log.warning("MCP サーバー（config.toml の [hands] url）が未設定なので、Dot から呼び出せません")
    await run_until_restart(config, store, assistant)


async def run_until_restart(config, store, assistant: Assistant) -> None:
    """起動したあとの確かめと、裏で回すもの（ジョブ・声・定期処理・MCP サーバー）。起動し直しを頼まれるまで待つ。"""
    job_loop = asyncio.create_task(assistant.job_loop())
    ask_loop = asyncio.create_task(assistant.ask_loop())
    schedule_loop: asyncio.Task | None = None
    questions_loop: asyncio.Task | None = None
    hands_loop: asyncio.Task | None = None
    try:
        # 入れ替えたあとの起動なら、その結果を読んで印を消す（消さないと deploy/run.sh が前の版に戻す。updates.py）
        assistant.take_update()
        # モジュールの、起動したときの処理（class Module の on_start。自己改善なら、入れ替えの結果を取り込みのスレッドに
        # 知らせ、途中で止まった直しを「中断」にする。入れ替えの結果は core.last_update で受け取る）
        await assistant.modules_started()
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
            from kei_agent.conversation import questions
            questions_loop = asyncio.create_task(questions.serve(assistant, config.a2a.orchestrator))
        if config.hands_url:
            # MCP サーバー。Dot・Claude Code などから作業場で AI を動かしてもらう（hands.py）
            from kei_agent.operations import hands_server
            hands_loop = asyncio.create_task(hands_server.serve(assistant, config.hands_url, config.hands_token))
        # 取り込みのあと、動いている作業がなくなると立つ。終了すると launchd が新しい版で起動する
        await assistant.restart_requested.wait()
    finally:
        job_loop.cancel()
        ask_loop.cancel()
        for loop in (schedule_loop, questions_loop, hands_loop):
            if loop is not None:
                loop.cancel()


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5


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


def main() -> None:
    setup_logging()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
