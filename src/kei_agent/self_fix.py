"""Slack から Kei Agent 自身を直す流れ（docs/architecture.md）。

#00_kei-agent で案を話し合い、合意したら worktree で直し、差分を見せてから main に取り込んで入れ替わる。
新しい要望は要約して公開の GitHub issue にし、取り込めたら閉じる（issues.py）。
部品（git の操作、確認、合図）は improve.py、柵は guard.py にある。

Assistant に混ぜて使う。self.slack、self.store、self.config、self.run などは Assistant のもの。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from kei_agent import guard, improve, issues, runner
from kei_agent.auto_messages import history_prompt
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve_selected
from kei_agent.request import Request
from kei_agent.slack_text import FAILED_PREFIX
from kei_agent.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

# 直している途中・取り込み待ち・入れ替え待ちの状態。直すのは一度に1つだけ
ACTIVE_STATUSES = ("working", "review", "restarting")


class SelfFix:
    async def improve(self, req: Request, ws: Workspace) -> runner.RunResult | None:
        """#00_kei-agent のやりとり。案を考えるときはコードを読むだけで、書き込めるのは一時ディレクトリだけ。"""
        if req.trigger == "message" and req.message_ts == req.thread_ts:
            # やり直しの回（上限・再起動）でも、最初の要望の文を残す
            row = self.store.request_improvement(req.channel, req.thread_ts, req.text, None)
            if not row["issue_number"]:
                # issue にするのは裏で進め、案を考えるのを待たせない
                self.spawn(self.file_issue(req, row["request"]))
        scratch = self.config.state_dir / "improve" / req.thread_ts
        scratch.mkdir(parents=True, exist_ok=True)
        ws = replace(ws, cwd=scratch)
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
            result = await self.run(req, ws)
        # 「着手」「取り込み」は、依頼者の投稿で始まった回の返事にあるときだけ受け付ける。
        # ジョブの完了や接続の許可で自動で再開した回の返事では動かない
        if req.trigger != "message":
            return result
        if improve.wants(result.text, improve.START_MARKER):
            await self.start_self_fix(req)
        elif improve.wants(result.text, improve.MERGE_MARKER):
            await self.merge_self_fix(req)
        return result

    async def _fix_failed(self, req: Request, detail: str, text: str) -> None:
        self.store.update_improvement(req.channel, req.thread_ts, status="failed", detail=detail)
        await self.post(req, f"{FAILED_PREFIX} {text}")

    async def recover_interrupted_fixes(self) -> int:
        """前回の起動で「直している途中」のまま終わったものを失敗にする。起動時に1回呼ぶ。

        これをしないと working が残り続け、次の直しに着手できない。
        """
        rows = self.store.improvements_in("working")
        for row in rows:
            self.store.update_improvement(row["channel"], row["thread_ts"], status="failed", detail="中断")
            try:
                req = Request(row["channel"], await self.channel_name(row["channel"]), row["thread_ts"], None, "")
                await self.post(req, f"{FAILED_PREFIX} 直している途中で Kei Agent が止まったので、中断したよ。"
                                     "続けるなら、もう一度「着手」まで進めて。")
            except Exception:
                log.warning("中断した直しを知らせられません", exc_info=True)
        return len(rows)

    async def _check_or_fail(self, req: Request, worktree: Path, base: str) -> bool:
        """柵に触れていないか、秘密情報が入っていないかを見る。だめなら失敗として知らせる。"""
        problems = await asyncio.to_thread(guard.check_change, worktree, base, "HEAD")
        if problems:
            await self._fix_failed(req, "; ".join(problems),
                                   "この差分は取り込めないよ:\n" + "\n".join(f"• {p}" for p in problems))
        return not problems

    async def start_self_fix(self, req: Request) -> None:
        """合意した案で、worktree を作って直し始める。直すのは1つずつ。"""
        messages, _ = await self.thread_messages(req.channel, req.thread_ts)
        answered = improve.owner_replies(messages, self.bot_user_id, req.thread_ts, req.message_ts)
        if answered < improve.REPLIES_BEFORE_START:
            # 案への「いいよ」と、「これで進めていい？」への「いいよ」の2回をもらってから動く
            await self.post(req, "念のため確認させて。この直し方で進めていい？")
            return
        others = [r for r in self.store.improvements_in(*ACTIVE_STATUSES) if r["thread_ts"] != req.thread_ts]
        if others:
            await self.post(req, f"{FAILED_PREFIX} 先に進んでいる直しがあるので、それを取り込んでから着手するね。")
            return
        row = self.store.improvement(req.channel, req.thread_ts)
        if row is not None and row["status"] in ACTIVE_STATUSES:
            return
        worktree, branch, base = await asyncio.to_thread(improve.create_worktree, self.config, req.thread_ts)
        self.store.start_improvement(req.channel, req.thread_ts, req.text,
                                     branch=branch, worktree=str(worktree), base_commit=base)
        await self.post(req, f"🛠 直し始めるね（`{branch}`、`{base[:7]}` から）。"
                             "テストが通るまでやって、変えた内容をここに出す。")
        self.spawn(self._self_fix(req, worktree, branch, base))

    async def _self_fix(self, req: Request, worktree: Path, branch: str, base: str) -> None:
        """想定外の例外でも working のまま残さず、失敗として知らせる。"""
        try:
            await self._run_self_fix(req, worktree, branch, base)
        except asyncio.CancelledError:
            raise  # 終了のとき。次の起動で recover_interrupted_fixes が失敗にする
        except Exception as e:
            log.exception("直している途中で失敗しました")
            detail = f"{type(e).__name__}: {e}"[:500]
            try:
                await self._fix_failed(req, detail, f"直している途中で止まったよ: {detail}")
            except Exception:
                log.warning("失敗を知らせられません", exc_info=True)
                self.store.update_improvement(req.channel, req.thread_ts, status="failed", detail=detail)

    async def _run_self_fix(self, req: Request, worktree: Path, branch: str, base: str) -> None:
        ws = Workspace(req.channel_name, ChannelKind.SELF_FIX, worktree)
        messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
        prompt = improve.FIX_PROMPT + history_prompt(messages, self.bot_user_id, "", None, dropped=dropped)
        ui = self.thread_ui(req)
        await ui.start()
        await ui.activity("改善中…")
        try:
            recipe = resolve_selected(self.config, self.store, "self_fix", UseCase.SELF_FIX_IMPLEMENTATION)
        except ModelPolicyError as e:
            await ui.finish("")
            await self._fix_failed(req, str(e), str(e))
            return
        with self.claude_running():
            result = await runner.run_model(
                self.config, runner.ExecutionRequest(ws, recipe, None, req.channel, req.thread_ts), prompt,
                ui.activity, ui.text,
            )
        await ui.finish(result.text, awaiting=True)
        if result.is_error:
            errors = "; ".join(result.errors)[:500]
            await self._fix_failed(req, errors, f"直している途中で止まったよ: {errors}")
            return
        commit = await asyncio.to_thread(improve.commit_all, worktree, improve.commit_message(req.text, result.text))
        if commit is None:
            await self._fix_failed(req, "変更なし", "変わったファイルがなかったよ。")
            return
        if not await self._check_or_fail(req, worktree, base):
            return
        self.store.update_improvement(req.channel, req.thread_ts, status="review")
        await self.upload_diff(req, worktree, base)
        await self.post(req, improve.review_summary(self.config, worktree, base, result.text, ""), markdown=True)

    async def upload_diff(self, req: Request, worktree: Path, base: str) -> None:
        diff = await asyncio.to_thread(improve.diff_text, worktree, base, "HEAD")
        try:
            await self.slack.files_upload_v2(
                channel=req.channel, thread_ts=req.thread_ts,
                file_uploads=[{"filename": "change.diff", "title": "change.diff", "content": diff[:900_000]}])
        except Exception:
            log.warning("差分を添付できません", exc_info=True)

    async def merge_self_fix(self, req: Request) -> None:
        """依頼者が「いいよ」と言った差分を、テストを回してから main に取り込み、push する。"""
        row = self.store.improvement(req.channel, req.thread_ts)
        if row is None or row["status"] != "review":
            await self.post(req, f"{FAILED_PREFIX} 取り込めるものが見つからないよ。")
            return
        worktree, branch, base = Path(row["worktree"]), row["branch"], row["base_commit"]
        if await asyncio.to_thread(improve.repo_dirty, self.config.repo_root):
            await self.post(req, f"{FAILED_PREFIX} 手元のリポジトリにコミットしていない変更があるよ。"
                                 "先にコミットしてから、もう一度「いいよ」と言って。")
            return
        if await asyncio.to_thread(improve.head, self.config.repo_root) != base:
            caught = await asyncio.to_thread(improve.catch_up_with_main, worktree)
            if not caught.ok:
                await self._fix_failed(req, caught.output[:500],
                                       f"main に合わせ直せなかったよ:\n```\n{caught.output[:1000]}\n```")
                return
            base = await asyncio.to_thread(improve.head, self.config.repo_root)
            self.store.update_improvement(req.channel, req.thread_ts, base_commit=base)
            await self.upload_diff(req, worktree, base)
            await self.post(req, "main が先に進んでいたので、その上に乗せ直したよ。差分を見て、もう一度「いいよ」と言って。")
            return
        ui = self.thread_ui(req)
        await ui.start()
        await ui.activity("取り込み中…")
        try:
            checks = await asyncio.to_thread(improve.run_checks, worktree)
        finally:
            await ui.finish("")
        if not checks.ok:
            await self._fix_failed(req, "確認が通らない",
                                   f"取り込む前の確認が通らなかったよ:\n```\n{checks.output[:2000]}\n```")
            return
        if not await self._check_or_fail(req, worktree, base):
            return
        try:
            merged = await asyncio.to_thread(improve.merge_and_push, self.config, branch)
        except improve.PushError as e:
            if e.undone:
                # 手元の main は元に戻した。worktree は残すので、もう一度「いいよ」でやり直せる
                await self.post(req, f"{FAILED_PREFIX} GitHub に push できなかったので、取り込みを取り消したよ。"
                                     f"もう一度「いいよ」と言えばやり直す。\n```\n{e.output[:1000]}\n```")
            else:
                await self._fix_failed(req, "push できず、手元の main も戻せなかった",
                                       "GitHub に push できず、手元の main も戻せなかったよ。"
                                       "手元の main が GitHub より進んだままなので、手で確かめて。"
                                       f"\n```\n{e.output[:1000]}\n```")
            return
        except RuntimeError as e:
            await self._fix_failed(req, str(e)[:500], f"main に取り込めなかったよ:\n```\n{str(e)[:1000]}\n```")
            return
        improve.mark_pending(self.config, base, req.thread_ts)
        self.store.update_improvement(req.channel, req.thread_ts, status="restarting", merge_commit=merged)
        await asyncio.to_thread(improve.remove_worktree, self.config, worktree, branch)
        await self.post(req, f"📦 取り込んで GitHub に push したよ（`{merged[:7]}`）。"
                             "動いている作業が終わったら、新しい版で起動し直す。")
        self.request_restart()

    def request_restart(self) -> None:
        """動いている claude の作業がなくなったら終了する（launchd が新しい版で起動し直す）。"""
        async def wait_then_restart() -> None:
            await self.idle.wait()
            # エージェントも同じリポジトリを読むので、一緒に入れ替える（本体だけだと古いまま動く）
            await asyncio.to_thread(improve.restart_agents, self.config)
            log.info("新しい版で起動し直すため、終了します")
            self.restart_requested.set()

        self.spawn(wait_then_restart())

    async def announce_update(self) -> None:
        """起動したときに、取り込みの結果をスレッドに知らせる。"""
        rolled = improve.read_rolled_back(self.config)
        if rolled is not None:
            previous, thread_ts = rolled
            row = self.store.improvement_by_thread(thread_ts) if thread_ts else None
            if row is not None:
                req = Request(row["channel"], await self.channel_name(row["channel"]), thread_ts, None, "")
                await self.post(req, f"{FAILED_PREFIX} 新しい版で起動できなかったので、`{previous[:7]}` に戻したよ。"
                                     "取り消しの内容は GitHub にも送った。ログを見て、直し方を考え直そう。")
                self.store.update_improvement(row["channel"], thread_ts, status="failed", detail="起動できなかった")
            await asyncio.to_thread(improve.push_revert, self.config)
            improve.rolled_back_path(self.config).unlink(missing_ok=True)
            return
        pending = improve.read_pending(self.config)
        if pending is None:
            return
        previous, thread_ts = pending
        improve.pending_path(self.config).unlink(missing_ok=True)
        row = self.store.improvement_by_thread(thread_ts) if thread_ts else None
        if row is None:
            return
        req = Request(row["channel"], await self.channel_name(row["channel"]), thread_ts, None, "")
        self.store.update_improvement(row["channel"], thread_ts, status="done")
        await self.post(req, f"✅ 新しい版で起動したよ（`{(row['merge_commit'] or '')[:7]}`）。"
                             f"うまくいかなければ `{previous[:7]}` に戻せる。")
        await self.close_issue(row)

    async def file_issue(self, req: Request, request: str) -> None:
        """新しい要望を、要約した公開の GitHub issue にする。原文は Slack に残し、issue にもファイルにも書かない。"""
        if not request.strip():
            await self.post(req, "要望の文がないので、GitHub の issue にはしなかったよ。")
            return
        try:
            # 作って番号を残すまでは、入れ替え（再起動）を待たせる。途中で止まると issue が二重にできる
            with self.claude_running():
                summary = await issues.summarize(self.config, self.store, request)
                issue = await issues.create(self.config, summary)
                self.store.request_improvement(req.channel, req.thread_ts, request, issue.number)
        except issues.NoProvider:
            await self.post(req, "自己改善の AI（Claude か Codex）がまだ選ばれていないので、要望は GitHub の issue に"
                                 "しなかったよ。App Home の設定で選んでね。")
            return
        except issues.IssueError as e:
            await self.notify_trouble(_trouble("要望を GitHub の issue にできませんでした", e))
            return
        except Exception as e:
            log.exception("要望を GitHub の issue にできません")
            await self.notify_trouble(f"要望を GitHub の issue にできませんでした: {type(e).__name__}: {e}")
            return
        await self.post(req, f"要望を要約して、公開の GitHub issue <{issue.url}|#{issue.number}> にしたよ"
                             "（元の文は載せていない）。")

    async def close_issue(self, row) -> None:
        """取り込めた要望の issue を、取り込んだコミットを添えて閉じる。issue にしていない要望は何もしない。"""
        number = row["issue_number"]
        if not number:
            return
        try:
            await issues.close(self.config, number, row["merge_commit"] or "")
        except issues.IssueError as e:
            await self.notify_trouble(_trouble(f"issue #{number} を閉じられませんでした", e))
        except Exception as e:
            # 起動の途中で呼ばれるので、何があっても起動は止めない
            log.exception("issue を閉じられません")
            await self.notify_trouble(f"issue #{number} を閉じられませんでした: {type(e).__name__}: {e}")


def _trouble(head: str, e: issues.IssueError) -> str:
    """知らせの文。改善チャンネルには理由まで、gh の出力などの中身はログにだけ残る（trouble_notice が「: 」の後ろを落とす）。"""
    return f"{head}（{e.reason}）" + (f": {e.detail}" if e.detail else "")
