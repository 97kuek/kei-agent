"""裏で回すもの: Slack の外から置かれた依頼（ask）と、研究のジョブの見張り。

Assistant に混ぜて使う。self.slack、self.store、self.config などは Assistant のもの。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from kei_agent.conversation import ask
from kei_agent.conversation.auto_messages import (
    job_resume_prompt,
    job_status_label,
)
from kei_agent.conversation.request import Request
from kei_agent.conversation.slack_text import (
    FAILED_PREFIX,
)
from kei_agent.execution.jobs import missing_outputs
from kei_agent.workspaces import themes

log = logging.getLogger(__name__)
# MCP の会話を記録するチャンネルの名前（hands.CHANNEL）
HANDS_CHANNEL = "mcp"


class BackgroundLoops:
    async def ask_loop(self) -> None:
        """同じ Mac に置かれた依頼を、数秒ごとに拾う。"""
        failing = False
        ask.recover_asks(self.config)
        while True:
            try:
                await self.handle_asks()
                failing = False
            except Exception as e:
                log.exception("外からの依頼を拾えませんでした")
                if not failing:
                    await self.notify_trouble(f"外からの依頼を拾えません: {type(e).__name__}: {e}")
                failing = True
            await asyncio.sleep(ask.POLL_SECONDS)

    async def handle_asks(self) -> None:
        pending = ask.claim_asks(self.config)
        if not pending:
            return
        try:
            ids = await self.channel_ids()
        except Exception:
            # 拾った依頼を「処理中」のまま置き去りにしない。戻して次の周回で拾い直す
            ask.recover_asks(self.config)
            raise
        for item in pending:
            try:
                theme = str(item.payload.get("theme") or "")
                text = str(item.payload.get("text") or "").strip()
                kind = item.payload.get("kind", "request")
                channel = ids.get(theme)
                if not channel or not text:
                    await self.notify_trouble(
                        f"外からの依頼を渡せませんでした（テーマ: {theme or '不明'}）: {text[:100] or '（空）'}")
                    ask.complete_ask(item)
                    continue
                header = "📌 声で決まったこと" if kind == "note" else "🎤 声からの依頼"
                thread_ts = item.payload.get("thread_ts")
                if thread_ts is not None and (not isinstance(thread_ts, str) or not thread_ts.strip()):
                    await self.notify_trouble(
                        f"外からの依頼を渡せませんでした（テーマ: {theme}）: 保存された Slack スレッドが不正です")
                    ask.complete_ask(item)
                    continue
                if thread_ts is None:
                    resp = await self.slack.chat_postMessage(channel=channel, text=f"{header}\n{text}")
                    thread_ts = resp["ts"]
                if "thread_ts" not in item.payload:
                    ask.record_thread(item, thread_ts)
                if kind == "note":
                    ask.complete_ask(item)
                    continue
                await self.submit(Request(
                    channel=channel, channel_name=theme, thread_ts=thread_ts, message_ts=None,
                    text=text, trigger="voice",
                ))
                ask.complete_ask(item)
            except Exception:
                ask.retry_ask(item)
                raise

    # 契約の上限（Claude AI usage limit）

    async def handle_job_requests(self, cwd: Path) -> None:
        for o in await self.jobs.process_requests(cwd):
            if o.channel == HANDS_CHANNEL:
                # MCPから頼まれたジョブは Slack に出さない（頭が jobs の道具で様子と結果を見る）
                if o.error:
                    log.warning("MCP のジョブの依頼を投入できませんでした: %s", o.error)
                continue
            # 依頼のチャンネルとスレッドは Claude が書いたものなので、知っているスレッドのときだけ投稿する
            known = bool(o.channel and o.thread_ts and self.store.get_thread(o.channel, o.thread_ts))
            req = Request(o.channel, "", o.thread_ts, None, "")
            if o.error:
                text = f"ジョブ「{o.job.name}」を投入できなかったよ: {o.error}" if o.job else o.error
                if known:
                    await self.post(req, f"{FAILED_PREFIX} {text}")
                else:
                    await self.notify_trouble(f"{cwd.name} のジョブの依頼を投入できなかった: {text}")
                continue
            # 投入できた依頼は、JobManager がスレッドを確かめてある（jobs._check_thread）
            await self.post(req, f"🧪 ジョブ {o.job.id}「{o.job.name}」を投入したよ: `{o.job.command}`")

    async def poll_jobs(self) -> None:
        """テーマのディレクトリに残った依頼を処理し、終わったジョブを報告する。"""
        root = self.config.research_root
        folders = [p for p in root.iterdir()] if root.is_dir() else []
        # 既存のフォルダを使うテーマ（themes.toml）のジョブの依頼も拾う
        folders += [p for p in themes.places(self.config).values() if p not in folders]
        for cwd in sorted(p for p in folders if (p / ".kei-agent" / "requests").is_dir()):
            await self.handle_job_requests(cwd)
        for job in await self.jobs.refresh():
            self.jobs.mark_reported(job)
            row = self.store.get_thread(job.channel, job.thread_ts)
            if row is None:
                continue
            if job.channel == HANDS_CHANNEL:
                await self.tell_head_job_done(job, row["channel_name"])
                continue
            req = Request(job.channel, row["channel_name"], job.thread_ts, None, "")
            missing = missing_outputs(job)
            note = f"。ただ {'、'.join(missing)} ができていない" if missing else ""
            try:
                await self.post(req, f"🧪 ジョブ {job.id}「{job.name}」が終わったよ"
                                     f"（{job_status_label(job.status)}{note}）。結果を見てみるね")
                await self.submit(replace(
                    req, text=job_resume_prompt(job), trigger="job",
                    outputs_since=job.submitted_at, awaiting_after=job.status != "succeeded" or bool(missing),
                ))
            except Exception:
                # 知らせられなかったジョブを黙って落とさない（ほかのジョブの報告は続ける）
                log.exception("ジョブの終わりを知らせられませんでした")
                await self.notify_trouble(f"ジョブ {job.id}「{job.name}」（#{row['channel_name']}）は終わりましたが、"
                                          f"スレッドに知らせられませんでした（{job_status_label(job.status)}）")

    async def tell_head_job_done(self, job, workspace: str) -> None:
        """MCPから投げたジョブが終わった。Slack につないでいないときは、頭への知らせにする（頭が同じ会話で続きを頼む）。
        Slack につないでいるときは、頭が jobs の道具で見る。"""
        from kei_agent.conversation.outbox import Outbox

        if not isinstance(self.slack, Outbox):
            return
        missing = missing_outputs(job)
        note = f"。ただ {'、'.join(missing)} ができていない" if missing else ""
        await self.slack.chat_postMessage(
            channel=workspace,
            text=f"🧪 ジョブ {job.id}「{job.name}」が終わったよ（{job_status_label(job.status)}{note}）。"
                 f"続きは run（workspace={workspace}, conversation={job.thread_ts}）に「ジョブの結果を読んでまとめて」と頼んでね")

    async def job_loop(self) -> None:
        failing = False
        while True:
            # やり直しが落ちても、ジョブの確認は止めない
            try:
                await self.retry_deferred()
            except Exception:
                log.exception("定期のやり直しに失敗しました: retry_deferred")
            try:
                await self.poll_jobs()
                failing = False
            except Exception as e:
                log.exception("ジョブの確認に失敗しました")
                if not failing:
                    # 失敗が続いている間は、最初の1回だけ知らせる
                    await self.notify_trouble(f"ジョブの状態を確認できません（pueue が止まっていませんか）: {type(e).__name__}: {e}")
                failing = True
            await asyncio.sleep(self.config.job_poll_seconds)

