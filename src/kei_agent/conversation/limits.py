"""AI の利用上限: 上限で止まった依頼を覚えて、明けたらやり直す。止まっている間の問い合わせに答える。

Assistant に混ぜて使う。self.slack、self.store、self.config などは Assistant のもの。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime

from kei_agent.conversation.auto_messages import (
    interrupted_prompt,
)
from kei_agent.conversation.request import Request
from kei_agent.conversation.slack_text import (
    FAILED_PREFIX,
    HOLD_REACTION,
    format_duration,
    is_status_inquiry,
)
from kei_agent.execution import agents
from kei_agent.storage import settings
from kei_agent.workspaces import themes
from kei_agent.workspaces.theme_files import (
    download_files,
)

log = logging.getLogger(__name__)


class LimitDeferral:
    async def hold_until_limit_ends(self, req: Request) -> bool:
        """上限で止まってやり直し待ちのスレッドに書かれたら、いまは動かさず、明けてからこの続きとしてやる（True）。

        すぐ動かすと、同じ上限にまた当たって知らせが重なる。やり直しの予約をこの依頼に置き換え、依頼に ⏳ を付ける
        （投稿はしない）。進み具合を聞かれただけなら、止まっている理由をその場で答える。そのスレッドの担当の AI が
        もう上限でなければ（明けた・別の provider に切り替えた）、何もしない（False。予約を取り消して動かす）。
        """
        pending = self.store.pending_deferred_for(req.channel, req.thread_ts)
        if not pending:
            return False
        try:
            ws = themes.resolve(self.config, req.channel_name)
        except ValueError:
            return False
        actor = self.actor_for(req, ws)
        provider = settings.selected_provider(self.config, self.store, actor)
        until = self.store.limit_until(provider) if provider else 0.0
        if until <= time.time():
            return False
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        if is_status_inquiry(req.text):
            await self.post(req, f"{provider} の利用上限で止まっているよ。{when} ごろに続きからやるね。")
            await self.mark_answered(req, failed=False)
            return True
        # 添付は、控えに残せる形（保存した場所）にしてから待たせる。止まった依頼の添付も引き継ぐ
        saved = await download_files(req.files, ws.cwd, self.bot_token) if req.files and ws.cwd is not None else []
        earlier = [path for _, payload in pending for path in payload.get("saved_files") or []]
        req = replace(req, files=[], saved_files=list(dict.fromkeys([*earlier, *req.saved_files, *saved])))
        # 待たせた依頼の ⏳ は、やり直すときに外す（前に待たせたものも覚えておく）
        held = [ts for _, payload in pending for ts in payload.get("held") or []]
        for deferred_id, _ in pending:
            self.store.finish_deferred(deferred_id)
        payload = {**req.to_payload(), "provider": provider,
                   "held": held + ([req.message_ts] if req.message_ts else [])}
        self.store.defer_run("request", payload, until)
        if req.message_ts:
            await self._react(self.slack.reactions_add, req.channel, req.message_ts, HOLD_REACTION)
        return True

    async def drop_deferred_for(self, req: Request) -> None:
        """上限で止まって自動でやり直す予定だった依頼を、このスレッドのぶんだけ取り消す。

        依頼者が「続けて」と書いたあとに、同じ依頼が裏でもう一度走ると、二重に作業してしまう。
        """
        canceled = [deferred_id for deferred_id, _ in self.store.pending_deferred_for(req.channel, req.thread_ts)]
        for deferred_id in canceled:
            self.store.finish_deferred(deferred_id)
        if canceled:
            await self.post(req, "上限で止まっていた依頼は、自動のやり直しをやめて、この続きとして進めるね。")

    async def tell_if_waiting(self, req: Request) -> bool:
        """同じスレッドの前の作業が続いているときは、黙って待たせずに一言返す。

        「今どんな感じ？」のような進み具合を尋ねるだけの一言は、新しい依頼としてキューの
        後ろに積まず、いまの状況をその場で組み立てて即答する（True を返し、以降の処理は行わない）。
        """
        if req.trigger not in ("message", "voice"):
            return False
        locked = self.thread_locks[(req.channel, req.thread_ts)].locked()
        jobs = [j for j in self.store.active_jobs() if j.channel == req.channel and j.thread_ts == req.thread_ts]
        if not locked and not jobs:
            return False
        if is_status_inquiry(req.text):
            await self.post(req, self._busy_status_text(req, locked, jobs))
            await self.mark_answered(req, failed=False)
            return True
        if locked:
            await self.post(req, "いま前の作業をしているから、終わったら取りかかるね。")
        return False

    def _busy_status_text(self, req: Request, locked: bool, jobs: list) -> str:
        """まだのこと・止まっている理由を、いまの状況から短く組み立てる。"""
        now = time.time()
        lines = []
        if locked:
            run = next((r for r in self.store.open_runs()
                       if r["channel"] == req.channel and r["thread_ts"] == req.thread_ts), None)
            if run:
                lines.append(f"まだ前の依頼を claude が処理してるよ（{format_duration(now - run['started_at'])}経過）。"
                             "終わったらこのまま返事するね。")
            else:
                lines.append("まだ前の依頼を claude が処理してるよ。終わったらこのまま返事するね。")
        for job in jobs:
            started = job.started_at or job.submitted_at
            lines.append(f"ジョブ「{job.name}」もまだ動いてるよ（{format_duration(now - started)}経過）。"
                         "終わったら知らせるね。")
        return "\n".join(lines)

    def limit_until(self, reset_at: float, now: float | None = None) -> float:
        """いつやり直すか。明ける時刻が古いまま返ることがあるので、過去ならしばらく待つ。"""
        now = time.time() if now is None else now
        if reset_at <= now:
            return now + self.LIMIT_FALLBACK_SECONDS
        return reset_at + self.LIMIT_MARGIN_SECONDS

    async def note_limit(self, reply: agents.Reply, agent: str, provider: str) -> None:
        """エージェントが上限に当たったことを、本体の1か所に集める（約束は本体が持つ）。

        provider ごとに待つ・やり直すの管理をオーケストレーターで持つ。
        """
        if reply.limit_reset_at is None:
            return
        if not provider:
            return
        until = self.limit_until(reply.limit_reset_at)
        if until <= self.store.limit_until(provider):
            return
        self.store.set_limit_until(provider, until)
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        await self.notify_trouble(f"{agent} の {provider} 利用上限に当たりました。{when} ごろまで待ちます。")

    async def defer_for_limit(self, req: Request, reset_at: float, provider: str, *, mention: bool = False) -> None:
        """上限に達した依頼を、明けてからやり直すものとして覚えておく。mention なら、知らせに依頼者へのメンションを付ける。"""
        until = self.limit_until(reset_at)
        if not provider:
            raise ValueError("provider が未選択です")
        self.store.set_limit_until(provider, max(self.store.limit_until(provider), until))
        self.store.defer_run("request", {**req.to_payload(), "provider": provider}, until)
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        text = f"{FAILED_PREFIX} {provider} の利用上限に達したみたい。{when} ごろに自動でやり直すね。"
        await self.post(req, f"<@{self.config.allowed_user_id}> {text}" if mention else text)
        self.emit("limited", reset_at=datetime.fromtimestamp(until).isoformat(timespec="minutes"))

    # 再起動で途中で止まった依頼

    async def resume_interrupted(self) -> int:
        """前回の終了時に動いていた依頼を、スレッドに一言添えてやり直す。"""
        interrupted = self.store.interrupted_requests()
        self.store.end_open_runs()
        for deferred_id, payload in interrupted:
            self.store.finish_deferred(deferred_id)
            req = Request.from_payload(payload)
            if not req.text.strip():
                continue
            if req.trigger == "handoff":
                # 引き継ぎは、普通の依頼として投げ直すと会話が続くだけになる。もう一度区切らせる
                try:
                    await self.post(req, "🧵 入れ替えで引き継ぎが途中で止まったので、もう一度まとめるね。")
                except Exception:
                    log.warning("中断を知らせられません", exc_info=True)
                self.spawn(self.hand_off(req))
                continue
            try:
                await self.post(req, f"{FAILED_PREFIX} さっきの作業は Kei Agent の入れ替えで途中で止まっちゃった。"
                                     "いまの状態を確かめて、続きからやり直すね。")
            except Exception:
                log.warning("中断を知らせられません", exc_info=True)
            # 元のメッセージの 👀 は残したまま。やり直しが終われば ✅ か ⚠️ に変わる
            await self.submit(replace(req, text=interrupted_prompt(req.text), retried=True))
        if interrupted:
            log.info("再起動で止まっていた依頼を %d 件やり直します", len(interrupted))
        return len(interrupted)

    async def retry_deferred(self, now: float | None = None) -> None:
        """上限で止まった依頼を、明けたらやり直す。"""
        now = time.time() if now is None else now
        for deferred_id, payload in self.store.due_deferred("request", now):
            # 1件ずつ。やり直せなかったものは黙って消さず、知らせる（ほかの依頼のやり直しは続ける）
            self.store.finish_deferred(deferred_id)
            try:
                await self._retry_one(payload)
            except Exception:
                log.exception("上限で止まった依頼をやり直せませんでした")
                await self.notify_trouble(f"上限で止まっていた依頼（#{payload.get('channel_name', '?')}）を、"
                                          "明けたあとにやり直せませんでした。もう一度頼んでください")

    async def _retry_one(self, payload: dict) -> None:
        req = replace(Request.from_payload(payload), retried=True)
        for ts in payload.get("held") or []:
            await self._react(self.slack.reactions_remove, req.channel, ts, HOLD_REACTION)
        original_provider = payload.get("provider")
        if original_provider:
            actor = self.actor_for(req, themes.resolve(self.config, req.channel_name))
            if settings.selected_provider(self.config, self.store, actor) != original_provider:
                await self.post(req, "使うモデルが切り替わったので、この依頼は自動で再実行しなかったよ。必要ならもう一度頼んでね。")
                return
        await self.submit(req)

    # ジョブ

