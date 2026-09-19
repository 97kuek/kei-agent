"""Slack の出来事を受けて、claude -p とジョブを動かし、スレッドに返す。

Slack Bolt に依存しないようにし、Slack API は `slack`（AsyncWebClient と同じメソッドを持つもの）として受け取る。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import aiohttp

from ezra import home, runner, settings, themes
from ezra.config import Config
from ezra.jobs import JobManager, log_tail
from ezra.notion import NotionError
from ezra.notion_store import NotionStore
from ezra.store import Job, Store
from ezra.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

PROGRESS_PREFIX = "⏳"
DONE_PREFIX = "✅"
FAILED_PREFIX = "⚠️"
MAX_UPLOADS = 10
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Slack から受け取って inputs/ に保存する1ファイルの上限
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
SLACK_TEXT_LIMIT = 11000

NIGHT_REACTION = "crescent_moon"
DONE_REACTION = "white_check_mark"
SEEN_REACTION = "eyes"
# 作業の手順（task_update）の見出しと補足の長さ
TASK_TITLE_LIMIT = 150
TASK_DETAILS_LIMIT = 300
FAILED_REACTION = "warning"
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


def message_text(message: dict) -> str:
    """メッセージの本文。`markdown_text` や流して見せた返事は text が空で blocks に入る。"""
    text = message.get("text") or ""
    if text:
        return text
    parts = []
    for block in message.get("blocks") or []:
        if block.get("type") == "markdown" and block.get("text"):
            parts.append(block["text"])
        for element in block.get("elements") or []:
            for item in element.get("elements") or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
    return "\n".join(parts)


def history_prompt(messages: list[dict], bot_user_id: str, new_text: str, exclude_ts: str | None) -> str:
    """セッションが失われたときに、スレッドの履歴から文脈を復元するためのプロンプト。"""
    lines = []
    for m in messages:
        text = message_text(m)
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


def domain_resume_prompt(decisions) -> str:
    """接続先の申し出に依頼者が答えたあと、会話を再開するときに渡す文。"""
    lines = ["[Ezra からの自動メッセージ] 接続先の申し出に、依頼者が答えました。"]
    for d in decisions:
        if d["status"] == "allowed":
            lines.append(f"- `{d['domain']}`: 許可されました。次のコマンドからつながります")
        else:
            lines.append(f"- `{d['domain']}`: 断られました。このテーマでは使えません")
    lines.append("止まっていた作業を続けてください。断られたものがあれば、別の入手先を探すか、"
                 "ここで止めてどうするかを報告してください。")
    return "\n".join(lines)


class ThreadUI:
    """作業中の見せ方。Slack の AI アプリ（Agent）向けの API を使う。

    - `agents.sessions.setStatus`: スレッドの状態を切り替える。processing（作業中。Slack が「Working...」を出す）、
      active（次の依頼待ち）、suspended（依頼者の返事待ち）のどれか。自由な文章は出せない
    - `chat.startStream` / `appendStream` / `stopStream`: 返事を流して見せる。道具を使うたびに作業の手順
      （task_update）を1行ずつ足し、最後にまとめを本文として出す

    途中で Claude が書いた独り言は、本文には出さず、次の手順の補足にする。本文に流すと、
    最後のまとめと同じ話が2回並ぶ。

    どちらもワークスペースや App の設定によっては使えないので、失敗したら黙って
    今までどおりの投稿に戻す（1回目だけログに残す）。
    """

    def __init__(self, slack, channel: str, thread_ts: str, team_id: str = "", user_id: str = ""):
        self.slack = slack
        self.channel = channel
        self.thread_ts = thread_ts
        self.team_id = team_id
        self.user_id = user_id
        self.status_ok = True
        self.stream_ok = True
        self.stream_ts: str | None = None
        self.task: dict | None = None  # いま実行中の手順
        self.task_count = 0
        self.narration = ""  # 次の手順に添える独り言

    async def start(self) -> None:
        await self._status("processing")

    async def activity(self, activity: str) -> None:
        """道具を呼ぶたびに呼ばれる。前の手順を完了にし、新しい手順を実行中として足す。"""
        chunks = self._complete_task()
        self.task_count += 1
        self.task = {"type": "task_update", "id": f"step-{self.task_count}",
                     "title": activity[:TASK_TITLE_LIMIT], "status": "in_progress"}
        if self.narration:
            self.task["details"] = self.narration[:TASK_DETAILS_LIMIT]
            self.narration = ""
        await self._stream(chunks=chunks + [self.task])

    async def text(self, chunk: str) -> None:
        """Claude が書いた文章。最後のまとめは finish() で本文にするので、ここでは次の手順の補足として取っておく。"""
        if chunk.strip():
            self.narration = chunk.strip()

    async def finish(self, answer: str, awaiting: bool = False) -> bool:
        """手順を閉じてまとめを本文に出し、スレッドを次の依頼待ち（返事待ちなら suspended）に戻す。

        流して見せられていれば True（結果をもう一度投稿しない）。
        """
        chunks = self._complete_task()
        if chunks:
            await self._stream(chunks=chunks)
        for piece in split_text(answer) if answer.strip() else []:
            await self._stream(markdown_text=piece)
        streamed = False
        if self.stream_ts is not None:
            try:
                await self.slack.chat_stopStream(channel=self.channel, ts=self.stream_ts)
                streamed = self.stream_ok and bool(answer.strip())
            except Exception:
                log.warning("流して見せた返事を終われません", exc_info=True)
        await self._status("suspended" if awaiting else "active")
        return streamed

    async def keep_working(self) -> None:
        """ジョブが走っている間は、返事を終えたあとも作業中に見せておく。"""
        await self._status("processing")

    def _complete_task(self) -> list[dict]:
        if self.task is None:
            return []
        done = self.task | {"status": "complete"}
        self.task = None
        return [done]

    async def _stream(self, markdown_text: str | None = None, chunks: list[dict] | None = None) -> None:
        if not self.stream_ok:
            return
        content = {"markdown_text": markdown_text} if markdown_text else {"chunks": chunks}
        try:
            if self.stream_ts is None:
                resp = await self.slack.chat_startStream(
                    channel=self.channel,
                    thread_ts=self.thread_ts,
                    recipient_team_id=self.team_id or None,
                    recipient_user_id=self.user_id or None,
                    task_display_mode="timeline",
                    **content,
                )
                self.stream_ts = resp["ts"]
            else:
                await self.slack.chat_appendStream(channel=self.channel, ts=self.stream_ts, **content)
        except Exception:
            log.warning("返事を流して見せられないので、まとめて投稿します", exc_info=True)
            self.stream_ok = False

    async def _status(self, status: str) -> None:
        if not self.status_ok:
            return
        try:
            await self.slack.agents_sessions_setStatus(channel_id=self.channel, thread_ts=self.thread_ts, status=status)
        except Exception:
            # App の「Agent experience」が有効でないと使えない
            log.info("スレッドのステータスを出せません", exc_info=True)
            self.status_ok = False


class ThemeRuns:
    """同じテーマで、同時に動いているスレッドを覚えておく。

    `outputs/` はテーマで共通なので、2つのスレッドが同時に動くと、隣が作った図を
    自分のスレッドに添付してしまう。時刻では自分の図と隣の図を区別できないため、
    「重なっていた」ことだけを覚えておき、結果に一言添える。
    """

    def __init__(self):
        self.running: dict[str, set[str]] = defaultdict(set)
        self.overlapped: set[tuple[str, str]] = set()

    def begin(self, theme: str, thread_ts: str) -> None:
        peers = self.running[theme]
        for peer in peers:
            self.overlapped.add((theme, peer))
            self.overlapped.add((theme, thread_ts))
        peers.add(thread_ts)

    def end(self, theme: str, thread_ts: str) -> bool:
        """終わったことを記録し、重なっていたなら True を返す。"""
        self.running[theme].discard(thread_ts)
        if not self.running[theme]:
            self.running.pop(theme, None)
        overlapped = (theme, thread_ts) in self.overlapped
        self.overlapped.discard((theme, thread_ts))
        return overlapped


class Assistant:
    def __init__(self, config: Config, store: Store, slack, jobs: JobManager, bot_token: str, bot_user_id: str,
                 notion: NotionStore | None = None, team_url: str = "", team_id: str = ""):
        self.config = config
        self.notion = notion
        # チャンネルへのリンクを作るのに使う（例: https://example.slack.com/）
        self.team_url = team_url
        # 返事を流して見せるときに要る（chat.startStream）
        self.team_id = team_id
        self.store = store
        self.slack = slack
        self.jobs = jobs
        self.bot_token = bot_token
        self.bot_user_id = bot_user_id
        self.semaphore = asyncio.Semaphore(config.max_concurrent_runs)
        self.thread_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.channel_names: dict[str, str] = {}
        # この起動で Notion に登録済みのテーマ（招待の取りこぼしを、使うときに埋める）
        self.registered_themes: set[str] = set()
        # 同じテーマで重なって動いたかを覚えておく（outputs/ が共通なので図が混ざる）
        self.theme_runs = ThemeRuns()
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
        # thread_broadcast は「以下にも投稿する」をつけた返信
        if event.get("subtype") not in (None, "file_share", "thread_broadcast") or event.get("bot_id"):
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
        self.registered_themes.add(name)
        if ws.kind is ChannelKind.IMPROVE:
            text = f"Ezra です。このチャンネルでメンションされた要望は `{self.config.backlog_path}` に記録します。"
        elif ws.kind is ChannelKind.OVERVIEW:
            text = f"Ezra です。このチャンネルでは、すべてのテーマを読んで相談に乗ります。書き込みは `{ws.cwd}` だけにします。"
        else:
            state = "作りました" if created else "使います"
            text = (
                f"Ezra です。このチャンネルのテーマ用に `{ws.cwd}` を{state}。"
                "研究の前提を `CLAUDE.md` に書いておくと、依頼のたびに説明しなくて済みます。"
            )
            await self.register_theme(channel, ws)
        await self.slack.chat_postMessage(channel=channel, text=text)

    async def on_channel_rename(self, event: dict) -> None:
        """チャンネル名が変わったら、覚えている名前を捨てる（テーマの対応がずれないように）。"""
        channel = (event.get("channel") or {}).get("id")
        if channel:
            self.channel_names.pop(channel, None)

    async def on_channel_archive(self, event: dict) -> None:
        """テーマのチャンネルをアーカイブしたら、そのテーマで許可した接続先を消す。"""
        channel = event.get("channel")
        if not channel:
            return
        name = await self.channel_name(channel)
        settings.drop_theme(self.store, name)

    # App Home（設定画面。docs/plan.md の11章）

    def _theme_names(self) -> list[str]:
        return [p.name for p in themes.theme_dirs(self.config)]

    async def publish_home(self, user_id: str) -> None:
        view = home.build_home(self.config, self.store, self._theme_names(),
                               is_owner=user_id == self.config.allowed_user_id)
        await self.slack.views_publish(user_id=user_id, view=view)

    async def on_home_opened(self, event: dict) -> None:
        if event.get("tab", "home") == "home" and event.get("user"):
            await self.publish_home(event["user"])

    async def on_home_action(self, body: dict) -> None:
        """App Home のボタンと時刻の選択。変えられるのは依頼者だけ。"""
        user = body.get("user", {}).get("id")
        if user != self.config.allowed_user_id:
            return
        action = (body.get("actions") or [{}])[0]
        action_id = action.get("action_id", "")
        kind, _, name = action_id.partition(":")
        if kind == "ezra_home_remove_domain":
            theme, _, domain = action.get("value", "").partition("\t")
            settings.remove_domain(self.store, theme, domain)
        elif kind == "ezra_home_add_domain":
            await self.slack.views_open(trigger_id=body.get("trigger_id"),
                                        view=home.build_add_domain_modal(self._theme_names()))
            return
        elif kind == "ezra_home_time" and name in settings.SCHEDULE_NAMES:
            _, enabled = settings.schedule_setting(self.config, self.store, name)
            settings.set_schedule(self.store, name, action.get("selected_time", ""), enabled)
        elif kind == "ezra_home_toggle" and name in settings.SCHEDULE_NAMES:
            hhmm, enabled = settings.schedule_setting(self.config, self.store, name)
            settings.set_schedule(self.store, name, hhmm, not enabled)
        else:
            return
        await self.publish_home(user)

    async def on_add_domain(self, body: dict) -> dict | None:
        """「接続先を足す」の送信。入力がおかしければ、欄ごとの説明を返す（モーダルに出す）。"""
        user = body.get("user", {}).get("id")
        if user != self.config.allowed_user_id:
            return {"domain": "依頼者だけが変えられます"}
        theme, domain = home.read_add_domain(body.get("view", {}))
        if theme not in self._theme_names():
            return {"theme": "テーマを選んでください"}
        if not settings.valid_domain(domain, allow_wildcard=True):
            return {"domain": "zenodo.org や *.example.com のように、ドメイン名だけを書いてください"}
        settings.allow_domain(self.store, theme, domain, "App Home から追加")
        await self.publish_home(user)
        return None

    async def register_theme(self, channel: str, ws: Workspace) -> None:
        if self.notion is None:
            return
        slack_url = f"{self.team_url}archives/{channel}" if self.team_url else ""
        try:
            await asyncio.to_thread(self.notion.ensure_theme, ws.channel_name, slack_url, f"{ws.cwd}/")
        except NotionError as e:
            await self.notify_trouble(f"Notion にテーマ「{ws.channel_name}」を登録できませんでした: {e}")

    async def notify_trouble(self, text: str) -> None:
        """うまくいかなかったことを、Ezra の改善のチャンネルに知らせる。"""
        log.warning(text)
        try:
            ids = await self.channel_ids()
            channel = next((ids[n] for n in self.config.improve_channels if n in ids), None)
            if channel:
                await self.slack.chat_postMessage(channel=channel, text=f"{FAILED_PREFIX} {text[:2500]}")
        except Exception:
            log.exception("Ezra の改善のチャンネルに知らせられません")

    async def permalink(self, channel: str, ts: str) -> str:
        resp = await self.slack.chat_getPermalink(channel=channel, message_ts=ts)
        return resp["permalink"]

    async def on_reaction_added(self, event: dict) -> None:
        """自分のメッセージに 🌙 をつけると、夜間の Task になる。"""
        item = event.get("item") or {}
        if event.get("reaction") != NIGHT_REACTION or item.get("type") != "message":
            return
        if not self.is_allowed(event.get("user")) or event.get("item_user") != event.get("user"):
            return
        channel, ts = item["channel"], item["ts"]
        name = await self.channel_name(channel)
        try:
            ws = themes.resolve(self.config, name)
        except ValueError:
            return
        if ws.kind is not ChannelKind.THEME:
            return
        message = await self.fetch_message(channel, ts) or {}
        req = Request(channel, name, message.get("thread_ts") or ts, None, "")
        if self.notion is None:
            await self.post(req, f"{FAILED_PREFIX} Notion が設定されていないので、今夜の Task にできません")
            return
        text = clean_text(message.get("text", ""))
        title = (text.splitlines() or ["Slack からの Task"])[0][:60] or "Slack からの Task"
        link = await self.permalink(channel, ts)
        quoted = "\n".join(f"> {line}" for line in text.splitlines()) or "> （本文なし）"
        body = f"Slack で 🌙 をつけて作った Task。\n\n{quoted}\n\n元のメッセージ: {link}"
        try:
            task = await asyncio.to_thread(self.notion.create_night_task, title, name, link, body)
        except NotionError as e:
            await self.post(req, f"{FAILED_PREFIX} Notion に Task を作れませんでした")
            await self.notify_trouble(f"🌙 の Task を Notion に作れませんでした: {e}")
            return
        await self.post(req, f"🌙 今夜の Task にしました: <{task.url}|{task.title}>")

    async def on_reaction_removed(self, event: dict) -> None:
        item = event.get("item") or {}
        if event.get("reaction") != NIGHT_REACTION or item.get("type") != "message":
            return
        if not self.is_allowed(event.get("user")) or event.get("item_user") != event.get("user"):
            return
        if self.notion is None:
            return
        link = await self.permalink(item["channel"], item["ts"])
        try:
            await asyncio.to_thread(self.notion.cancel_night_task, link)
        except NotionError as e:
            await self.notify_trouble(f"🌙 を外した Task を Notion で取り消せませんでした: {e}")

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
        # セッションが作れなかった日でも、このスレッドへの返信には反応できるようにする
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
                await self.slack.reactions_add(channel=req.channel, timestamp=req.message_ts, name=SEEN_REACTION)
            except Exception:
                log.debug("リアクションをつけられません", exc_info=True)
        task = asyncio.create_task(self._process_and_report(req))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _process_and_report(self, req: Request) -> None:
        """process() が落ちても、👀 がついたまま黙って終わらないようにする。"""
        try:
            await self.process(req)
        except Exception as e:
            log.exception("依頼の処理が落ちました")
            try:
                await self.post(req, f"{FAILED_PREFIX} 依頼の処理が落ちました: `{type(e).__name__}: {e}`")
            except Exception:
                log.exception("落ちたことをスレッドに伝えられません")
            await self.notify_trouble(f"#{req.channel_name} の依頼の処理が落ちました: {type(e).__name__}: {e}")

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
        if ws.kind is ChannelKind.THEME:
            ws = replace(ws, allowed_domains=tuple(settings.theme_domains(self.store, ws.channel_name)))
        if ws.kind is ChannelKind.THEME and ws.channel_name not in self.registered_themes:
            # 招待のイベントを取りこぼしていても、1テーマ = 1チャンネル = 1ディレクトリ = Notion の1行を保つ
            self.registered_themes.add(ws.channel_name)
            await self.register_theme(req.channel, ws)
        # スレッドごとのロックは捨てずに残す。「待っている依頼がいるか」は release の直後に
        # 一瞬だけ「いない」と見えるので、そこで捨てると、待っていた依頼が別のロックを取り、
        # 同じスレッド（同じセッション）の claude が2本同時に走る
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
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

        who = {"job": "Ezra（ジョブ完了）", "domain": "Ezra（接続先の返事）"}.get(req.trigger, "依頼者")
        append_thread_log(ws.cwd, req.channel_name, req.thread_ts, who, req.text + (
            "\n\n" + "\n".join(f"- 添付: `{p}`" for p in saved) if saved else ""))

        ui = ThreadUI(self.slack, req.channel, req.thread_ts, self.team_id, self.config.allowed_user_id)
        await ui.start()
        self.theme_runs.begin(req.channel_name, req.thread_ts)
        before = snapshot_outputs(ws.cwd)
        run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)

        result = await runner.run_claude(
            self.config, ws, prompt, session_id, req.channel, req.thread_ts, ui.activity, ui.text
        )
        if result.session_missing:
            replies = await self.slack.conversations_replies(channel=req.channel, ts=req.thread_ts, limit=200)
            prompt = history_prompt(replies.get("messages", []), self.bot_user_id, prompt, req.message_ts)
            result = await runner.run_claude(
                self.config, ws, prompt, None, req.channel, req.thread_ts, ui.activity, ui.text
            )

        self.store.end_run(run_id, result.is_error, result.cost_usd)
        if result.session_id:
            self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, result.session_id)
        connect = self.new_connect_requests(ws, result.text)
        awaiting = req.awaiting_after or result.is_error or AWAITING_MARKER in result.text or bool(connect)
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        await self.sync_review_conclusion(req)
        streamed = await ui.finish(result.text, awaiting and not result.is_error)

        if result.text:
            append_thread_log(ws.cwd, req.channel_name, req.thread_ts, "Ezra", result.text)
            if not streamed:  # 流して見せられなかったときだけ、まとめて投稿する
                for chunk in split_text(result.text):
                    await self.post(req, chunk, markdown=True)
        if result.is_error:
            reason = "上限時間を超えたので止めました" if result.timed_out else "; ".join(result.errors)[:1500]
            await self.post(req, f"{FAILED_PREFIX} エラーで止まりました: {reason or '原因不明'}")

        overlapped = self.theme_runs.end(req.channel_name, req.thread_ts)
        try:
            new_files = changed_files(before, snapshot_outputs(ws.cwd), req.outputs_since)
            await self.upload_outputs(req, ws.cwd, new_files)
            if new_files and overlapped:
                # 時刻では自分の図と隣の図を区別できないので、混ざりうることを黙って隠さない
                await self.post(req, f"{FAILED_PREFIX} このテーマで別のスレッドも動いていたので、"
                                     "別のスレッドの図が混ざっているかもしれません。")
        except Exception as e:
            # 結果はもう返しているので、添付だけ失敗したことを伝える
            log.exception("outputs/ のファイルを添付できません")
            await self.post(req, f"{FAILED_PREFIX} `outputs/` のファイルを添付できませんでした: `{type(e).__name__}: {e}`")
        await self.mark_answered(req, result.is_error)
        await self.ask_for_domains(req, ws, connect)
        await self.handle_job_requests(ws.cwd)
        if any(j.channel == req.channel and j.thread_ts == req.thread_ts for j in self.store.active_jobs()):
            await ui.keep_working()
        return result

    async def mark_answered(self, req: Request, failed: bool) -> None:
        """答えた依頼の 👀 を外し、✅（止まったときは ⚠️）をつける。どの依頼に答えたかが一目で分かる。"""
        if not req.message_ts:
            return
        for method, name in ((self.slack.reactions_remove, SEEN_REACTION),
                             (self.slack.reactions_add, FAILED_REACTION if failed else DONE_REACTION)):
            try:
                await method(channel=req.channel, timestamp=req.message_ts, name=name)
            except Exception:
                log.debug("リアクションを変えられません", exc_info=True)

    # 接続先の申し出（docs/plan.md の11章）

    def new_connect_requests(self, ws: Workspace, text: str) -> list[tuple[str, str]]:
        """返答の `🔒 接続:` のうち、まだ許可していないもの。テーマ以外では受け付けない。"""
        if ws.kind is not ChannelKind.THEME:
            return []
        allowed = set(self.config.allowed_domains) | set(ws.allowed_domains)
        return [(d, reason) for d, reason in settings.parse_connect_requests(text) if d not in allowed]

    async def ask_for_domains(self, req: Request, ws: Workspace, requests: list[tuple[str, str]]) -> None:
        for domain, reason in requests:
            req_id = settings.add_request(self.store, req.channel, req.thread_ts, ws.channel_name, domain, reason)
            text = f"🔒 `{domain}` につなぎたいそうです" + (f"。理由: {reason}" if reason else "")
            await self.slack.chat_postMessage(
                channel=req.channel, thread_ts=req.thread_ts, text=text,
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text":
                        f"許可すると、#{ws.channel_name} の作業でだけつながります（チャンネルをアーカイブすると消えます）"}]},
                    {"type": "actions", "elements": [
                        {"type": "button", "action_id": "ezra_domain_allow", "style": "primary",
                         "text": {"type": "plain_text", "text": "許可する"}, "value": str(req_id)},
                        {"type": "button", "action_id": "ezra_domain_deny",
                         "text": {"type": "plain_text", "text": "断る"}, "value": str(req_id)},
                    ]},
                ],
            )

    async def on_domain_action(self, body: dict) -> None:
        """[許可する] [断る] が押された。押せるのは依頼者だけ。"""
        if body.get("user", {}).get("id") != self.config.allowed_user_id:
            return
        action = (body.get("actions") or [{}])[0]
        allowed = action.get("action_id") == "ezra_domain_allow"
        try:
            req_id = int(action.get("value", ""))
        except ValueError:
            return
        if not settings.resolve_request(self.store, req_id, "allowed" if allowed else "denied"):
            return  # もう決まっている（2度押し）
        row = settings.get_request(self.store, req_id)
        if allowed:
            settings.allow_domain(self.store, row["theme"], row["domain"], row["reason"] or "")
        done = f"🔒 `{row['domain']}` への接続を" + ("許可しました" if allowed else "断りました")
        container = body.get("container", {})
        try:
            await self.slack.chat_update(
                channel=container.get("channel_id") or row["channel"], ts=container.get("message_ts"),
                text=done, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": done}}])
        except Exception:
            log.warning("接続先の申し出のメッセージを書き換えられません", exc_info=True)
        if settings.pending_requests(self.store, row["channel"], row["thread_ts"]):
            return  # ほかの申し出に答えてから、まとめて再開する
        decisions = settings.take_decisions(self.store, row["channel"], row["thread_ts"])
        if decisions:
            await self.submit(Request(
                channel=row["channel"], channel_name=row["theme"], thread_ts=row["thread_ts"],
                message_ts=None, text=domain_resume_prompt(decisions), trigger="domain",
            ))

    async def sync_review_conclusion(self, req: Request) -> None:
        """振り返りのスレッドに貼られた結論を、Notion の振り返りページにも追記する。"""
        if req.trigger != "message" or self.notion is None:
            return
        link = self.store.notion_link(req.channel, req.thread_ts)
        if link is None or link["kind"] != "review" or not req.text.strip():
            return
        stamp = datetime.now().strftime("%m/%d %H:%M")
        try:
            await asyncio.to_thread(self.notion.append_markdown, link["page_id"],
                                    f"### Slack に貼った結論（{stamp}）\n\n{req.text}")
        except NotionError as e:
            await self.notify_trouble(f"振り返りの結論を Notion に追記できませんでした: {e}")

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
                if int(f.get("size") or 0) > MAX_DOWNLOAD_BYTES:
                    log.warning("大きすぎる添付は保存しません: %s", f.get("name"))
                    continue
                name = _UNSAFE_FILENAME.sub("_", f.get("name") or f.get("id") or "file").lstrip(".") or "file"
                dest = inputs / name
                stem, suffix, n = dest.stem, dest.suffix, 1
                while dest.exists():
                    dest = inputs / f"{stem}-{n}{suffix}"
                    n += 1
                async with http.get(url) as resp:
                    resp.raise_for_status()
                    # 全部をメモリに載せない
                    with dest.open("wb") as out:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            out.write(chunk)
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
            path.write_text(f"# Ezra への要望\n\n`#{req.channel_name}` で受け付けた要望。新しいものが下。\n", encoding="utf-8")
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
        await self.post(req, f"要望を `{path}` に記録しました。")

    # ジョブ

    async def handle_job_requests(self, cwd: Path) -> None:
        for o in await self.jobs.process_requests(cwd):
            # 依頼のチャンネルとスレッドは Claude が書いたものなので、知っているスレッドのときだけ投稿する
            known = bool(o.channel and o.thread_ts and self.store.get_thread(o.channel, o.thread_ts))
            req = Request(o.channel, "", o.thread_ts, None, "")
            if o.error:
                text = (f"ジョブ「{o.job.name}」を投入できませんでした: {o.error}") if o.job else o.error
                if known:
                    await self.post(req, f"{FAILED_PREFIX} {text}")
                else:
                    await self.notify_trouble(f"`{cwd}` のジョブの依頼: {text}")
                continue
            await self.post(req, f"🧪 ジョブ {o.job.id}「{o.job.name}」を投入しました: `{o.job.command}`")

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
        failing = False
        while True:
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
