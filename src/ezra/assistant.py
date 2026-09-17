"""Slack の出来事を受けて、claude -p とジョブを動かし、スレッドに返す。

Slack Bolt に依存しないようにし、Slack API は `slack`（AsyncWebClient と同じメソッドを持つもの）として受け取る。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp

from ezra import runner, themes
from ezra.config import Config
from ezra.jobs import JobManager, log_tail
from ezra.store import Job, Store
from ezra.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

PROGRESS_PREFIX = "⏳"
DONE_PREFIX = "✅"
FAILED_PREFIX = "⚠️"
PROGRESS_INTERVAL_SECONDS = 3.0
MAX_UPLOADS = 10
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SLACK_TEXT_LIMIT = 11000

NIGHT_REACTION = "crescent_moon"
DONE_REACTION = "white_check_mark"
# Claude が依頼者の判断を待つときに、返答の最後の行をこれで始める（prompts/system.md）
AWAITING_MARKER = "❓ 確認:"

_MENTION = re.compile(r"<@[A-Z0-9]+>")
_UNSAFE_FILENAME = re.compile(r"[^\w.\-]+")


@dataclass
class Request:
    channel: str
    channel_name: str
    thread_ts: str
    # リアクションをつける元のメッセージ。ジョブ完了で再開するときは None
    message_ts: str | None
    text: str
    trigger: str = "message"
    files: list[dict] = field(default_factory=list)
    # これ以降に更新された outputs/ のファイルも添付する（ジョブが作ったファイル用）
    outputs_since: float | None = None
    # 終わったあと、依頼者の返事待ちとして扱う（失敗したジョブのあとなど）
    awaiting_after: bool = False


def clean_text(text: str) -> str:
    return _MENTION.sub("", text or "").strip()


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes}分"


def split_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    """Slack の文字数上限に合わせて、なるべく段落の切れ目で分ける。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks or [""]


def snapshot_outputs(cwd: Path) -> dict[Path, tuple[float, int]]:
    outputs = cwd / "outputs"
    if not outputs.is_dir():
        return {}
    return {
        p: (p.stat().st_mtime, p.stat().st_size)
        for p in outputs.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


def changed_files(
    before: dict[Path, tuple[float, int]], after: dict[Path, tuple[float, int]], since: float | None = None
) -> list[Path]:
    return sorted(
        p for p, stat in after.items()
        if before.get(p) != stat or (since is not None and stat[0] >= since)
    )


def append_thread_log(cwd: Path, channel_name: str, thread_ts: str, who: str, text: str) -> None:
    """スレッドのやり取りをテーマのディレクトリにも残す。Codex App から Slack を見ずに経緯を読めるようにする。"""
    path = cwd / ".ezra" / "threads" / f"{thread_ts}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        started = datetime.fromtimestamp(float(thread_ts)).strftime("%Y-%m-%d %H:%M")
        path.write_text(f"# #{channel_name} のスレッド（{started} 開始）\n", encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {who}（{stamp}）\n\n{text.strip()}\n")


def job_resume_prompt(job: Job) -> str:
    elapsed = ""
    if job.started_at and job.finished_at:
        elapsed = f"（実行時間 {format_duration(job.finished_at - job.started_at)}）"
    status = {"succeeded": "成功", "failed": "失敗", "cancelled": "取り消し"}.get(job.status, job.status)
    detail = f"\n詳細: {job.detail}" if job.detail else ""
    tail = log_tail(job)
    tail_block = f"\n\nログの末尾:\n```\n{tail}\n```" if tail else ""
    return (
        f"[Ezra からの自動メッセージ] ジョブ {job.id}「{job.name}」が終わりました。"
        f"結果: {status}{elapsed}{detail}\n"
        f"実行したもの: {job.command}\nログ: logs/job-{job.id}.log{tail_block}\n\n"
        "ezra:job skill の「ジョブが終わって会話が再開されたとき」に沿って、結果を確認して報告してください。"
    )


def history_prompt(messages: list[dict], bot_user_id: str, new_text: str, exclude_ts: str | None) -> str:
    """セッションが失われたときに、スレッドの履歴から文脈を復元するためのプロンプト。"""
    lines = []
    for m in messages:
        text = m.get("text") or ""
        if m.get("ts") == exclude_ts or text.startswith((PROGRESS_PREFIX, DONE_PREFIX, FAILED_PREFIX)):
            continue
        who = "Ezra" if m.get("user") == bot_user_id or m.get("bot_id") else "依頼者"
        lines.append(f"{who}: {clean_text(text)}")
    history = "\n\n".join(lines)
    return (
        "[Ezra からの自動メッセージ] このスレッドの以前の会話セッションが見つからないため、"
        "Slack のスレッドの履歴から文脈を復元します。\n\n"
        f"<thread_history>\n{history}\n</thread_history>\n\n"
        f"続きの依頼:\n{new_text}"
    )


class Progress:
    """経過用メッセージを、間隔をあけて書き換える。"""

    def __init__(self, slack, channel: str, thread_ts: str):
        self.slack = slack
        self.channel = channel
        self.thread_ts = thread_ts
        self.ts: str | None = None
        self.started = time.monotonic()
        self.activities: list[str] = []
        self.last_update = 0.0

    def _text(self, header: str) -> str:
        recent = self.activities[-5:]
        body = "\n".join(f"• {a}" for a in recent)
        more = f"（ほか {len(self.activities) - len(recent)} 件）\n" if len(self.activities) > len(recent) else ""
        return f"{header}\n{more}{body}".rstrip()

    async def start(self) -> None:
        resp = await self.slack.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=f"{PROGRESS_PREFIX} 作業を始めます"
        )
        self.ts = resp["ts"]

    async def add(self, activity: str) -> None:
        self.activities.append(activity)
        if time.monotonic() - self.last_update >= PROGRESS_INTERVAL_SECONDS:
            await self._update(f"{PROGRESS_PREFIX} 作業中（{format_duration(time.monotonic() - self.started)}）")

    async def finish(self, ok: bool) -> None:
        prefix, word = (DONE_PREFIX, "作業しました") if ok else (FAILED_PREFIX, "作業が止まりました")
        await self._update(f"{prefix} {format_duration(time.monotonic() - self.started)} {word}")

    async def _update(self, header: str) -> None:
        if self.ts is None:
            return
        self.last_update = time.monotonic()
        try:
            await self.slack.chat_update(channel=self.channel, ts=self.ts, text=self._text(header))
        except Exception:  # 経過の更新に失敗しても作業は続ける
            log.exception("経過メッセージを更新できません")


class Assistant:
    def __init__(self, config: Config, store: Store, slack, jobs: JobManager, bot_token: str, bot_user_id: str):
        self.config = config
        self.store = store
        self.slack = slack
        self.jobs = jobs
        self.bot_token = bot_token
        self.bot_user_id = bot_user_id
        self.semaphore = asyncio.Semaphore(config.max_concurrent_runs)
        self.thread_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.channel_names: dict[str, str] = {}
        self.tasks: set[asyncio.Task] = set()

    # Slack の出来事

    def is_allowed(self, user: str | None) -> bool:
        return bool(self.config.allowed_user_id) and user == self.config.allowed_user_id

    async def channel_name(self, channel: str) -> str:
        if channel not in self.channel_names:
            info = await self.slack.conversations_info(channel=channel)
            self.channel_names[channel] = info["channel"]["name"]
        return self.channel_names[channel]

    async def on_mention(self, event: dict) -> None:
        if not self.is_allowed(event.get("user")):
            return
        channel = event["channel"]
        await self.submit(Request(
            channel=channel,
            channel_name=await self.channel_name(channel),
            thread_ts=event.get("thread_ts") or event["ts"],
            message_ts=event["ts"],
            text=clean_text(event.get("text", "")),
            files=event.get("files") or [],
        ))

    async def on_message(self, event: dict) -> None:
        """スレッド内の、メンションなしの返信。Ezra が動いているスレッドだけに反応する。"""
        if event.get("subtype") not in (None, "file_share") or event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == event.get("ts"):
            return
        if f"<@{self.bot_user_id}>" in (event.get("text") or ""):
            return  # app_mention で処理する
        if not self.is_allowed(event.get("user")):
            return
        channel = event["channel"]
        if self.store.get_thread(channel, thread_ts) is None:
            return
        await self.submit(Request(
            channel=channel,
            channel_name=await self.channel_name(channel),
            thread_ts=thread_ts,
            message_ts=event["ts"],
            text=clean_text(event.get("text", "")),
            files=event.get("files") or [],
        ))

    async def on_member_joined(self, event: dict) -> None:
        if event.get("user") != self.bot_user_id:
            return
        channel = event["channel"]
        name = await self.channel_name(channel)
        try:
            ws = themes.resolve(self.config, name)
        except ValueError as e:
            await self.slack.chat_postMessage(channel=channel, text=f"{FAILED_PREFIX} {e}")
            return
        created = themes.ensure_workspace(ws)
        if ws.kind is ChannelKind.IMPROVE:
            text = "Ezra です。このチャンネルでメンションされた要望は `docs/backlog.md` に記録します。"
        elif ws.kind is ChannelKind.OVERVIEW:
            text = f"Ezra です。このチャンネルでは、すべてのテーマを読んで相談に乗ります。書き込みは `{ws.cwd}` だけにします。"
        else:
            state = "作りました" if created else "使います"
            text = (
                f"Ezra です。このチャンネルのテーマ用に `{ws.cwd}` を{state}。"
                "研究の前提を `CLAUDE.md` に書いておくと、依頼のたびに説明しなくて済みます。"
            )
        await self.slack.chat_postMessage(channel=channel, text=text)

    async def on_reaction_added(self, event: dict) -> None:
        """自分のメッセージに 🌙 をつけると、夜間の Task になる。"""
        item = event.get("item") or {}
        if event.get("reaction") != NIGHT_REACTION or item.get("type") != "message":
            return
        if not self.is_allowed(event.get("user")) or event.get("item_user") != event.get("user"):
            return
        name = await self.channel_name(item["channel"])
        try:
            if themes.resolve(self.config, name).kind is not ChannelKind.THEME:
                return
        except ValueError:
            return
        self.store.add_night_task(item["channel"], item["ts"])

    async def on_reaction_removed(self, event: dict) -> None:
        item = event.get("item") or {}
        if event.get("reaction") != NIGHT_REACTION or not self.is_allowed(event.get("user")):
            return
        self.store.remove_pending_night_task(item.get("channel", ""), item.get("ts", ""))

    async def fetch_message(self, channel: str, ts: str) -> dict | None:
        resp = await self.slack.conversations_replies(channel=channel, ts=ts, inclusive=True, limit=200)
        for m in resp.get("messages", []):
            if m.get("ts") == ts:
                return m
        return None

    async def channel_ids(self) -> dict[str, str]:
        """Ezra が参加しているチャンネルの、名前から ID への対応。"""
        ids: dict[str, str] = {}
        cursor = None
        while True:
            resp = await self.slack.conversations_list(
                types="public_channel,private_channel", exclude_archived=True, limit=200, cursor=cursor
            )
            for c in resp.get("channels", []):
                if c.get("is_member"):
                    ids[c["name"]] = c["id"]
                    self.channel_names[c["id"]] = c["name"]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return ids

    async def run_detached(self, ws: Workspace, channel_name: str, prompt: str, trigger: str) -> runner.RunResult:
        """スレッドを作らずに claude -p を動かす（定期処理用）。結果を見てから投稿先を決める。"""
        assert ws.cwd is not None
        themes.ensure_workspace(ws)
        async with self.semaphore:
            run_id = self.store.start_run("", "", channel_name, trigger)
            result = await runner.run_claude(self.config, ws, prompt, None, "", "")
            self.store.end_run(run_id, result.is_error, result.cost_usd)
        return result

    async def publish(self, channel: str, channel_name: str, ws: Workspace, header: str,
                      result: runner.RunResult, footer: str = "") -> str:
        """見出しをチャンネルに投稿し、結果をそのスレッドに返す。スレッドで続きを話せるようにする。"""
        assert ws.cwd is not None
        resp = await self.slack.chat_postMessage(channel=channel, text=header)
        thread_ts = resp["ts"]
        req = Request(channel, channel_name, thread_ts, None, "")
        if result.session_id:
            self.store.upsert_thread(channel, thread_ts, channel_name, result.session_id)
        if result.text:
            append_thread_log(ws.cwd, channel_name, thread_ts, "Ezra", result.text)
            for chunk in split_text(result.text):
                await self.post(req, chunk, markdown=True)
        if result.is_error:
            await self.post(req, f"{FAILED_PREFIX} エラーで止まりました: {'; '.join(result.errors)[:1500] or '原因不明'}")
        if footer:
            await self.post(req, footer)
        return thread_ts

    async def react_done(self, channel: str, ts: str) -> None:
        try:
            await self.slack.reactions_add(channel=channel, timestamp=ts, name=DONE_REACTION)
        except Exception:
            log.debug("リアクションをつけられません", exc_info=True)

    # 依頼の処理

    async def submit(self, req: Request) -> None:
        """依頼を受け付けて、裏で処理を始める。"""
        if req.trigger == "message":
            self.store.set_awaiting(req.channel, req.thread_ts, False)
        if req.message_ts:
            try:
                await self.slack.reactions_add(channel=req.channel, timestamp=req.message_ts, name="eyes")
            except Exception:
                log.debug("リアクションをつけられません", exc_info=True)
        task = asyncio.create_task(self.process(req))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def process(self, req: Request) -> runner.RunResult | None:
        try:
            ws = themes.resolve(self.config, req.channel_name)
        except ValueError as e:
            await self.post(req, f"{FAILED_PREFIX} {e}")
            return None
        if ws.kind is ChannelKind.IMPROVE:
            await self.record_backlog(req)
            return None
        themes.ensure_workspace(ws)
        async with self.thread_locks[(req.channel, req.thread_ts)]:
            async with self.semaphore:
                try:
                    return await self.run(req, ws)
                except Exception as e:
                    log.exception("依頼の処理に失敗しました")
                    await self.post(req, f"{FAILED_PREFIX} 内部エラーで止まりました: `{type(e).__name__}: {e}`")
                    return None

    async def run(self, req: Request, ws: Workspace) -> runner.RunResult:
        assert ws.cwd is not None
        saved = await self.save_files(req.files, ws.cwd)
        prompt = req.text
        if saved:
            prompt += "\n\n添付ファイル（保存先）:\n" + "\n".join(f"- {p}" for p in saved)

        # ジョブの依頼をこのスレッドのものとして確かめられるよう、先にスレッドを記録する
        row = self.store.get_thread(req.channel, req.thread_ts)
        self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, None)
        session_id = row["session_id"] if row else None

        who = "Ezra（ジョブ完了）" if req.trigger == "job" else "依頼者"
        append_thread_log(ws.cwd, req.channel_name, req.thread_ts, who, req.text + (
            "\n\n" + "\n".join(f"- 添付: `{p}`" for p in saved) if saved else ""))

        progress = Progress(self.slack, req.channel, req.thread_ts)
        await progress.start()
        before = snapshot_outputs(ws.cwd)
        run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)

        result = await runner.run_claude(
            self.config, ws, prompt, session_id, req.channel, req.thread_ts, progress.add
        )
        if result.session_missing:
            replies = await self.slack.conversations_replies(channel=req.channel, ts=req.thread_ts, limit=200)
            prompt = history_prompt(replies.get("messages", []), self.bot_user_id, prompt, req.message_ts)
            result = await runner.run_claude(
                self.config, ws, prompt, None, req.channel, req.thread_ts, progress.add
            )

        self.store.end_run(run_id, result.is_error, result.cost_usd)
        if result.session_id:
            self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, result.session_id)
        awaiting = req.awaiting_after or result.is_error or AWAITING_MARKER in result.text
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        await progress.finish(ok=not result.is_error)

        if result.text:
            append_thread_log(ws.cwd, req.channel_name, req.thread_ts, "Ezra", result.text)
            for chunk in split_text(result.text):
                await self.post(req, chunk, markdown=True)
        if result.is_error:
            reason = "上限時間を超えたので止めました" if result.timed_out else "; ".join(result.errors)[:1500]
            await self.post(req, f"{FAILED_PREFIX} エラーで止まりました: {reason or '原因不明'}")

        await self.upload_outputs(req, ws.cwd, changed_files(before, snapshot_outputs(ws.cwd), req.outputs_since))
        await self.handle_job_requests(ws.cwd)
        return result

    async def post(self, req: Request, text: str, markdown: bool = False) -> None:
        if markdown:
            await self.slack.chat_postMessage(channel=req.channel, thread_ts=req.thread_ts, markdown_text=text)
        else:
            await self.slack.chat_postMessage(channel=req.channel, thread_ts=req.thread_ts, text=text)

    async def save_files(self, files: list[dict], cwd: Path) -> list[str]:
        saved = []
        if not files:
            return saved
        inputs = cwd / "inputs"
        inputs.mkdir(exist_ok=True)
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.bot_token}"}) as http:
            for f in files:
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                name = _UNSAFE_FILENAME.sub("_", f.get("name") or f.get("id") or "file").lstrip(".") or "file"
                dest = inputs / name
                stem, suffix, n = dest.stem, dest.suffix, 1
                while dest.exists():
                    dest = inputs / f"{stem}-{n}{suffix}"
                    n += 1
                async with http.get(url) as resp:
                    resp.raise_for_status()
                    dest.write_bytes(await resp.read())
                saved.append(str(dest.relative_to(cwd)))
        return saved

    async def upload_outputs(self, req: Request, cwd: Path, files: list[Path]) -> None:
        if not files:
            return
        uploadable = [p for p in files if p.stat().st_size <= MAX_UPLOAD_BYTES][:MAX_UPLOADS]
        skipped = [p for p in files if p not in uploadable]
        if uploadable:
            await self.slack.files_upload_v2(
                channel=req.channel,
                thread_ts=req.thread_ts,
                file_uploads=[{"file": str(p), "filename": p.name, "title": str(p.relative_to(cwd))} for p in uploadable],
            )
        if skipped:
            names = "\n".join(f"• `{p.relative_to(cwd)}`" for p in skipped)
            await self.post(req, f"添付しなかったファイル（数か大きさの上限を超えたもの）:\n{names}")

    async def record_backlog(self, req: Request) -> None:
        path = self.config.backlog_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# Ezra への要望\n\n`#assistant-improve` で受け付けた要望。新しいものが下。\n", encoding="utf-8")
        link = ""
        try:
            resp = await self.slack.chat_getPermalink(channel=req.channel, message_ts=req.message_ts or req.thread_ts)
            link = f"（[Slack]({resp['permalink']})）"
        except Exception:
            log.debug("パーマリンクを取得できません", exc_info=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        body = req.text.replace("\n", "\n  ")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n- [ ] {stamp} {body} {link}\n")
        await self.post(req, "要望を `docs/backlog.md` に記録しました。")

    # ジョブ

    async def handle_job_requests(self, cwd: Path) -> None:
        for job, error in await self.jobs.process_requests(cwd):
            req = Request(job.channel, "", job.thread_ts, None, "")
            if error:
                # 依頼元のスレッドが確かめられないときは、どこにも投稿しない
                if job.channel and job.thread_ts and not error.startswith("依頼元"):
                    await self.post(req, f"{FAILED_PREFIX} ジョブ「{job.name}」を投入できませんでした: {error}")
                continue
            await self.post(req, f"🧪 ジョブ {job.id}「{job.name}」を投入しました: `{job.command}`")

    async def poll_jobs(self) -> None:
        """テーマのディレクトリに残った依頼を処理し、終わったジョブを報告する。"""
        root = self.config.research_root
        if root.is_dir():
            for cwd in sorted(p for p in root.iterdir() if (p / ".ezra" / "requests").is_dir()):
                await self.handle_job_requests(cwd)
        for job in await self.jobs.refresh():
            self.jobs.mark_reported(job)
            row = self.store.get_thread(job.channel, job.thread_ts)
            if row is None:
                continue
            req = Request(job.channel, row["channel_name"], job.thread_ts, None, "")
            status = {"succeeded": "成功", "failed": "失敗", "cancelled": "取り消し"}.get(job.status, job.status)
            await self.post(req, f"🧪 ジョブ {job.id}「{job.name}」が終わりました（{status}）。結果を確認します")
            await self.submit(Request(
                channel=job.channel,
                channel_name=row["channel_name"],
                thread_ts=job.thread_ts,
                message_ts=None,
                text=job_resume_prompt(job),
                trigger="job",
                outputs_since=job.submitted_at,
                awaiting_after=job.status != "succeeded",
            ))

    async def job_loop(self) -> None:
        while True:
            try:
                await self.poll_jobs()
            except Exception:
                log.exception("ジョブの確認に失敗しました")
            await asyncio.sleep(self.config.job_poll_seconds)
