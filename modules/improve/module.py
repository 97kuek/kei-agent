"""自己改善のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

Kei Agent のチャンネル（#00_kei-agent。module.toml の core_channels）で要望を聞き、Kei Agent 自身を直す。

- 新しい要望は、要約して公開の GitHub issue にする（原文は Slack に残す。issues.py）。取り込めたら閉じる
- 直さずに済んだら（もう直っていた・やらないと決まった）、AI が「✅ 解決済み」「🗑 見送り」と書き、要望を終わりにして
  issue を閉じる（見送りは not planned）
- 直し方を相談する（AI は Kei Agent のコードを読むだけで、書けるのは相談の作業用のフォルダだけ）
- 案に2回「いいよ」をもらい、AI が「🛠 着手」と書いたら、worktree で直す。差分を添付して、取り込んでいいか聞く
- 直したものに「いいよ」をもらい、AI が「📦 取り込み」と書いたら、テストを回し、柵を確かめて main に取り込み、push して、
  作業が終わってから新しい版で起動し直す（起動できなければ、本体が前の版に戻す）
- 起動したとき（on_start）: 途中で止まった直しを「中断」にし、入れ替えの結果を取り込みのスレッドに知らせる
- 見回り（tick）: 1日に1回、使い終わった worktree と相談の作業用のフォルダを片づけ、閉じられなかった issue を閉じ直す

直すのは一度に1つだけ。柵（guard.py・config.example.toml・deploy/）に触れた差分は取り込まない（core.check_change）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from kei_agent.api import (
    FAILED_PREFIX,
    AIError,
    Core,
    Request,
    Update,
    contains_secret,
    final_answer,
    is_status_inquiry,
)

from . import issues, repo
from .fixes import ACTIVE, Fix, Fixes

log = logging.getLogger(__name__)

# 合図の行（着手・取り込み・解決済み・見送り）。検出に使うだけで、Slack には出さない
HIDDEN = (repo.START_MARKER, repo.MERGE_MARKER, repo.RESOLVED_MARKER, repo.DROPPED_MARKER)
# 直さずに終わりにできる状態（直している途中と入れ替え待ちは除く。取り込み待ちの直しは捨てる）
CLOSABLE = ("planning", "review", "failed")
# 直さずに終わったときの detail（issue に添える言葉を選ぶのに使う）
RESOLVED = "直さずに解決"
DROPPED = "見送り"
# 案を出したまま動きのない相談の作業用のフォルダを、残しておく日数
TALK_KEEP_DAYS = 7


def trouble(head: str, e: issues.IssueError) -> str:
    """知らせの文。改善チャンネルには理由まで、gh の出力などの中身はログにだけ残る（知らせは「: 」の後ろを落とす）。"""
    return f"{head}（{e.reason}）" + (f": {e.detail}" if e.detail else "")


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.fixes = Fixes(core.records)
        # 片づけた日（1日に1回）
        self._cleaned = ""
        # 取り込んでいるスレッド（「いいよ」が重なっても、2回は取り込まない）
        self._merging: set[str] = set()

    @property
    def worktrees(self) -> Path:
        return self.core.state_dir / "worktrees"

    @property
    def talks(self) -> Path:
        return self.core.state_dir / "talk"

    def welcome(self) -> str:
        return ("このチャンネルでメンションされた要望は、要約して公開の GitHub issue にします（Slack の文はそのまま"
                "載せません）。直し方を相談して、よければ Kei Agent 自身を直して取り込みます。")

    async def _post(self, req: Request, text: str) -> None:
        await self.core.post(req.channel, text, thread_ts=req.thread_ts)

    async def _failed(self, req: Request, detail: str, text: str) -> None:
        """止まったことを知らせる。直す・取り込むのは時間がかかり、依頼者は待っていないので、メンションを付ける。"""
        self.fixes.update(req.thread_ts, status="failed", detail=detail)
        await self._post(req, self.core.mention(f"{FAILED_PREFIX} {text}"))

    # 相談

    async def on_message(self, req: Request, skill: str = "", params=None) -> None:
        """Kei Agent のチャンネルのやりとり。案を考えるときはコードを読むだけで、書けるのは相談の作業用のフォルダだけ。"""
        if req.trigger == "message" and req.message_ts == req.thread_ts:
            # やり直しの回（上限・再起動）でも、最初の要望の文を残す
            fix = self.fixes.request(req.channel, req.thread_ts, req.text)
            if fix.issue_number is None and not is_status_inquiry(fix.request):
                # issue にするのは裏で進め、案を考えるのを待たせない（様子を聞いただけの一言は、要望ではないので issue にしない）
                self.core.spawn(self.file_issue(req, fix.request))
        answer = await self.core.work(req, folder=self.talks / req.thread_ts, hide=HIDDEN)
        # 合図は、依頼者の投稿で始まった回の返事にあるときだけ受け付ける。ジョブの完了や接続の許可で自動で再開した回と、
        # 様子を聞かれただけの回（読むだけで動く）の返事では動かない
        if req.trigger != "message" or is_status_inquiry(req.text):
            return
        if repo.wants(answer, repo.START_MARKER):
            await self.start_fix(req)
        elif repo.wants(answer, repo.MERGE_MARKER):
            # テストは時間がかかるので、スレッドの順番待ちと同時実行の枠を空けて、裏で進める
            self.core.spawn(self.merge_fix(req))
        elif repo.wants(answer, repo.RESOLVED_MARKER):
            await self.close_without_fix(req, dropped=False)
        elif repo.wants(answer, repo.DROPPED_MARKER):
            await self.close_without_fix(req, dropped=True)

    # 直す

    async def start_fix(self, req: Request) -> None:
        """合意した案で、worktree を作って直し始める。直すのは1つずつ。"""
        messages = await self.core.thread_messages(req.channel, req.thread_ts)
        if repo.owner_replies(messages, self.core.is_owner, req.thread_ts, req.message_ts,
                              skip=is_status_inquiry) < repo.REPLIES_BEFORE_START:
            # 案への「いいよ」と、「これで進めていい？」への「いいよ」の2回をもらってから動く
            await self._post(req, "念のため確認させて。この直し方で進めていい？")
            return
        if any(fix.thread_ts != req.thread_ts for fix in self.fixes.in_status(*ACTIVE)):
            await self._post(req, f"{FAILED_PREFIX} 先に進んでいる直しがあるので、それを取り込んでから着手するね。")
            return
        fix = self.fixes.get(req.thread_ts)
        if fix is not None and fix.status in ACTIVE:
            return
        worktree, branch, base = await self.core.to_thread(repo.create_worktree, self.core.repo_root, self.worktrees,
                                                           req.thread_ts)
        self.fixes.start(req.channel, req.thread_ts, req.text, branch=branch, worktree=str(worktree), base_commit=base)
        await self._post(req, f"🛠 直し始めるね（`{branch}`、`{base[:7]}` から）。テストが通るまでやって、変えた内容をここに出す。")
        self.core.spawn(self._fix(req, worktree, base))

    async def _fix(self, req: Request, worktree: Path, base: str) -> None:
        """想定外の例外でも working のまま残さず、失敗として知らせる。"""
        try:
            await self._run_fix(req, worktree, base)
        except asyncio.CancelledError:
            raise  # 終了のとき。次の起動で on_start が「中断」にする
        except Exception as e:
            log.exception("直している途中で失敗しました")
            detail = f"{type(e).__name__}: {e}"[:500]
            try:
                await self._failed(req, detail, f"直している途中で止まったよ: {detail}")
            except Exception:
                log.warning("失敗を知らせられません", exc_info=True)
                self.fixes.update(req.thread_ts, status="failed", detail=detail)

    async def _run_fix(self, req: Request, worktree: Path, base: str) -> None:
        prompt = repo.FIX_PROMPT + await self.core.thread_history(req.channel, req.thread_ts)
        try:
            async with self.core.progress(req, "改善中…"):
                text = await self.core.run_ai("improve_fix", prompt, folder=worktree)
        except AIError as e:
            reason = str(e)[:500]
            await self._failed(req, reason, f"直している途中で止まったよ: {reason}")
            return
        summary = final_answer(text) or text
        fix = self.fixes.get(req.thread_ts)
        request = fix.request if fix is not None else req.text
        if await self.core.to_thread(repo.commit_all, worktree, repo.commit_message(request, summary)) is None:
            await self._failed(req, "変更なし", "変わったファイルがなかったよ。")
            return
        if not await self._fence(req, worktree, base):
            return
        self.fixes.update(req.thread_ts, status="review")
        await self._upload_diff(req, worktree, base)
        await self.core.post(req.channel, repo.review_summary(worktree, base, summary), thread_ts=req.thread_ts,
                             markdown=True)
        # 直している間、依頼者は待っていない。取り込んでいいか見てもらえるよう、メンションで知らせる
        await self._post(req, self.core.mention("直したよ。取り込んでいいか見てね"))

    async def _fence(self, req: Request, worktree: Path, base: str) -> bool:
        """柵に触れていないか、秘密情報が入っていないかを本体に確かめてもらう。だめなら失敗として知らせる。"""
        problems = await self.core.check_change(worktree, base)
        if problems:
            await self._failed(req, "; ".join(problems),
                               "この差分は取り込めないよ:\n" + "\n".join(f"• {p}" for p in problems))
        return not problems

    async def _upload_diff(self, req: Request, worktree: Path, base: str) -> None:
        diff = await self.core.to_thread(repo.diff_text, worktree, base)
        try:
            await self.core.upload(req.channel, req.thread_ts, "change.diff", diff[:900_000])
        except Exception:
            log.warning("差分を添付できません", exc_info=True)

    # 取り込む

    async def merge_fix(self, req: Request) -> None:
        """依頼者が「いいよ」と言った差分を、テストを回してから main に取り込み、push して起動し直す。"""
        if req.thread_ts in self._merging:
            return
        self._merging.add(req.thread_ts)
        try:
            await self._merge(req)
        finally:
            self._merging.discard(req.thread_ts)

    async def _merge(self, req: Request) -> None:
        fix = self.fixes.get(req.thread_ts)
        if fix is None or fix.status != "review":
            await self._post(req, f"{FAILED_PREFIX} 取り込めるものが見つからないよ。")
            return
        root, worktree, branch, base = self.core.repo_root, Path(fix.worktree), fix.branch, fix.base_commit
        if await self.core.to_thread(repo.repo_dirty, root):
            await self._post(req, f"{FAILED_PREFIX} 手元のリポジトリにコミットしていない変更があるよ。"
                                  "先にコミットしてから、もう一度「いいよ」と言って。")
            return
        if await self.core.to_thread(repo.head, root) != base:
            caught = await self.core.to_thread(repo.catch_up_with_main, worktree)
            if not caught.ok:
                await self._failed(req, caught.output[:500],
                                   f"main に合わせ直せなかったよ:\n```\n{caught.output[:1000]}\n```")
                return
            base = await self.core.to_thread(repo.head, root)
            self.fixes.update(req.thread_ts, base_commit=base)
            await self._upload_diff(req, worktree, base)
            await self._post(req, "main が先に進んでいたので、その上に乗せ直したよ。差分を見て、もう一度「いいよ」と言って。")
            return
        async with self.core.progress(req, "取り込み中…"):
            checks = await self.core.to_thread(repo.run_checks, worktree)
        if not checks.ok:
            await self._failed(req, "確認が通らない", f"取り込む前の確認が通らなかったよ:\n```\n{checks.output[:2000]}\n```")
            return
        if not await self._fence(req, worktree, base):
            return
        try:
            merged = await self.core.to_thread(repo.merge_and_push, root, branch)
        except repo.PushError as e:
            if e.undone:
                # 手元の main は元に戻した。worktree は残すので、もう一度「いいよ」でやり直せる
                await self._post(req, f"{FAILED_PREFIX} GitHub に push できなかったので、取り込みを取り消したよ。"
                                      f"もう一度「いいよ」と言えばやり直す。\n```\n{e.output[:1000]}\n```")
            else:
                await self._failed(req, "push できず、手元の main も戻せなかった",
                                   "GitHub に push できず、手元の main も戻せなかったよ。"
                                   "手元の main が GitHub より進んだままなので、手で確かめて。"
                                   f"\n```\n{e.output[:1000]}\n```")
            return
        except RuntimeError as e:
            await self._failed(req, str(e)[:500], f"main に取り込めなかったよ:\n```\n{str(e)[:1000]}\n```")
            return
        self.fixes.update(req.thread_ts, status="restarting", merge_commit=merged)
        await self.core.to_thread(repo.remove_worktree, root, worktree, branch)
        await self._post(req, f"📦 取り込んで GitHub に push したよ（`{merged[:7]}`）。"
                              "動いている作業が終わったら、新しい版で起動し直す。")
        # 新しい版で起動できなければ、本体が base に戻す。次の起動で、このスレッドに結果を知らせる（on_start）
        self.core.restart_for_update(base, req.thread_ts)

    # 直さずに終わる

    async def close_without_fix(self, req: Request, *, dropped: bool) -> None:
        """直さずに済んだ要望（もう直っていた・やらないと決まった）を終わりにし、issue を閉じる。

        最初の依頼への返事では閉じず、依頼者が一度答えてから。直している途中と入れ替え待ちのものはそのまま。
        取り込み待ちの直しは捨てる。
        """
        messages = await self.core.thread_messages(req.channel, req.thread_ts)
        if repo.owner_replies(messages, self.core.is_owner, req.thread_ts, req.message_ts, skip=is_status_inquiry) < 1:
            await self._post(req, "念のため確認させて。直さずに、この要望を終わりにしていい？")
            return
        fix = self.fixes.get(req.thread_ts)
        if fix is None:
            await self._post(req, f"{FAILED_PREFIX} この要望の記録が見つからないので、終わりにしなかったよ。")
            return
        if fix.status in ("done", "dropped"):
            await self._post(req, f"{FAILED_PREFIX} もう終わりにしてある要望だよ。")
            return
        if fix.status not in CLOSABLE or req.thread_ts in self._merging:
            await self._post(req, f"{FAILED_PREFIX} いま直している（取り込んでいる）ところなので、終わりにしなかったよ。")
            return
        if fix.status == "review" and fix.worktree:
            await self.core.to_thread(repo.remove_worktree, self.core.repo_root, Path(fix.worktree), fix.branch)
        how = DROPPED if dropped else RESOLVED
        fix = self.fixes.update(req.thread_ts, status="dropped" if dropped else "done", detail=how)
        if not fix.issue_number:
            await self._post(req, f"この要望は終わりにしたよ（{how}）。")
        elif await self._close_issue(fix):
            await self._post(req, f"この要望は終わりにして、issue #{fix.issue_number} を閉じたよ（{how}）。")
        else:
            await self._post(req, f"この要望は終わりにしたよ（{how}）。issue #{fix.issue_number} は閉じられなかったので、"
                                  "あとでやり直す。")

    # 起動したとき

    async def on_start(self) -> None:
        await self._recover()
        await self._announce(self.core.last_update())

    async def _recover(self) -> None:
        """前回の起動で「直している途中」のまま終わったものを失敗にする（残ると、次の直しに着手できない）。"""
        for fix in self.fixes.in_status("working"):
            self.fixes.update(fix.thread_ts, status="failed", detail="中断")
            try:
                await self.core.post(fix.channel, self.core.mention(
                    f"{FAILED_PREFIX} 直している途中で Kei Agent が止まったので、中断したよ。"
                    "続けるなら、もう一度「着手」まで進めて。"), thread_ts=fix.thread_ts)
            except Exception:
                log.warning("中断した直しを知らせられません", exc_info=True)

    async def _announce(self, update: Update | None) -> None:
        """入れ替えたあとの起動なら、その結果を取り込みのスレッドに知らせる（自分が頼んだ入れ替えのときだけ）。"""
        fix = self.fixes.get(update.note) if update is not None else None
        if update is None or fix is None:
            return
        if update.state == "rolled_back":
            await self.core.post(fix.channel, self.core.mention(
                f"{FAILED_PREFIX} 新しい版で起動できなかったので、`{update.previous[:7]}` に戻したよ。"
                "取り消しの内容は GitHub にも送った。ログを見て、直し方を考え直そう。"), thread_ts=fix.thread_ts)
            self.fixes.update(fix.thread_ts, status="failed", detail="起動できなかった")
            await self.core.to_thread(repo.push_revert, self.core.repo_root)
            return
        fix = self.fixes.update(fix.thread_ts, status="done")
        await self.core.post(fix.channel, self.core.mention(
            f"✅ 新しい版で起動したよ（`{fix.merge_commit[:7]}`）。うまくいかなければ `{update.previous[:7]}` に戻せる。"),
            thread_ts=fix.thread_ts)
        await self._close_issue(fix)

    # 要望の issue

    async def file_issue(self, req: Request, request: str) -> None:
        """新しい要望を、要約した公開の GitHub issue にする。原文は Slack に残し、issue にもファイルにも書かない。"""
        if not request.strip():
            await self._post(req, "要望の文がないので、GitHub の issue にはしなかったよ。")
            return
        if not self.core.provider:
            await self._post(req, "自己改善の AI（Claude か Codex）がまだ選ばれていないので、要望は GitHub の issue に"
                                  "しなかったよ。App Home の設定で選んでね。")
            return
        try:
            # 作って番号を残すまでは、入れ替え（再起動）を待たせる。途中で止まると issue が二重にできる
            with self.core.busy():
                summary = await issues.summarize(self.core.run_ai, request, contains_secret)
                issue = await issues.create(self.core.repo_root, summary)
                self.fixes.request(req.channel, req.thread_ts, request, issue.number)
        except issues.IssueError as e:
            await self.core.notify_trouble(trouble("要望を GitHub の issue にできませんでした", e))
            return
        except Exception as e:
            log.exception("要望を GitHub の issue にできません")
            await self.core.notify_trouble(f"要望を GitHub の issue にできませんでした: {type(e).__name__}: {e}")
            return
        await self._post(req, f"要望を要約して、公開の GitHub issue <{issue.url}|#{issue.number}> にしたよ"
                              "（元の文は載せていない）。")

    async def _close_issue(self, fix: Fix, *, notify: bool = True) -> bool:
        """終わった要望の issue を閉じる。閉じたら True。issue にしていない要望と、もう閉じたものは何もしない。

        取り込んだならそのコミットを、直さずに終わったならそう添える（見送りは not planned）。閉じられなければ
        知らせ（notify のとき。やり直しでは、ログにだけ残す）、見回り（tick）でやり直す。
        """
        if not fix.issue_number or fix.issue_closed:
            return False
        if fix.status == "dropped":
            commit, reason = "", issues.NOT_PLANNED
        else:
            commit, reason = ("" if fix.detail == RESOLVED else fix.merge_commit), issues.COMPLETED
        head = f"issue #{fix.issue_number} を閉じられませんでした"
        try:
            await issues.close(self.core.repo_root, fix.issue_number, commit=commit, reason=reason)
        except Exception as e:
            # 起動の途中でも呼ばれるので、何があっても止めない
            if isinstance(e, issues.IssueError):
                text = trouble(head, e)
            else:
                log.exception("issue を閉じられません")
                text = f"{head}: {type(e).__name__}: {e}"
            if notify:
                await self.core.notify_trouble(text)
            else:
                log.warning("%s", text)
            return False
        self.fixes.update(fix.thread_ts, issue_closed=True)
        return True

    # 片づけ

    async def tick(self, now: datetime) -> None:
        """1日に1回、使い終わった worktree と相談の作業用のフォルダを片づけ、閉じられなかった issue を閉じ直す。"""
        day = now.date().isoformat()
        if self._cleaned == day:
            return
        self._cleaned = day
        busy = self.fixes.in_status(*ACTIVE)
        recent = time.time() - TALK_KEEP_DAYS * 86400
        keep_worktrees = {Path(fix.worktree).name for fix in busy if fix.worktree}
        keep_talks = {fix.thread_ts for fix in busy}
        keep_talks |= {fix.thread_ts for fix in self.fixes.in_status("planning") if fix.updated_at > recent}
        removed = await self.core.to_thread(repo.clean, self.core.repo_root, self.worktrees, self.talks,
                                            keep_worktrees, keep_talks)
        if removed:
            log.info("使い終わった worktree を %d 件片づけました", removed)
        for fix in self.fixes.in_status("done", "dropped"):
            await self._close_issue(fix, notify=False)
