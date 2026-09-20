"""Slack の出来事を受けて、claude -p とジョブを動かし、スレッドに返す。

Slack Bolt に依存しないようにし、Slack API は `slack`（AsyncWebClient と同じメソッドを持つもの）として受け取る。
役割ごとの処理は、次のファイルに分けて Assistant に混ぜている。

- settings_actions.py: 接続先の申し出のボタンと App Home
- self_fix.py: Slack から Kei Agent 自身を直す流れ
- handoff.py: 長くなったスレッドを区切って、新しいスレッドで続ける
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Coroutine
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from kei_agent import (
    a2a,
    agents,
    ask,
    course,
    guard,
    improve,
    research,
    runner,
    settings,
    themes,
    work,
)
from kei_agent.auto_messages import (
    history_prompt,
    interrupted_prompt,
    job_resume_prompt,
    job_status_label,
    rules_update_prompt,
)
from kei_agent.config import Config
from kei_agent.course import CourseChannel
from kei_agent.handoff import Handoff, strip_handoff
from kei_agent.jobs import JobManager, missing_outputs
from kei_agent.notion import NotionError
from kei_agent.notion_store import NotionStore
from kei_agent.request import Request
from kei_agent.self_fix import SelfFix
from kei_agent.settings_actions import SettingsActions
from kei_agent.slack_text import (
    AWAITING_MARKER,
    DONE_REACTION,
    FAILED_PREFIX,
    FAILED_REACTION,
    NIGHT_REACTION,
    SEEN_REACTION,
    clean_text,
    format_duration,
    is_status_inquiry,
    split_text,
)
from kei_agent.store import Store
from kei_agent.theme_files import (
    append_thread_log,
    changed_files,
    download_files,
    snapshot_outputs,
    split_uploads,
)
from kei_agent.themes import ChannelKind, Workspace
from kei_agent.thread_ui import ThreadUI
from kei_agent.work import WorkChannel

log = logging.getLogger(__name__)

# これ以上かかった作業が終わったら、依頼者に通知の別投稿を送る（短い依頼には送らない）
NOTIFY_AFTER_SECONDS = 60
# 名刺（エージェントのスキル）を読み直す間隔。入れ替えても、これだけたてば新しいスキルを使える
SKILLS_TTL_SECONDS = 600
# スレッドの履歴を読むときの、1回あたりの件数と、プロンプトに載せる上限（新しいものを残す）
HISTORY_PAGE = 200
HISTORY_MAX_MESSAGES = 600
# 履歴を読むときのページ数の上限（とても長いスレッドで、いつまでも読み続けないように）
HISTORY_MAX_PAGES = 20
# スレッドのログに残すときの、依頼の出どころの呼び名
WHO_BY_TRIGGER = {"job": "Kei Agent（ジョブ完了）", "domain": "Kei Agent（接続先の返事）", "voice": "依頼者（声）"}


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


class Assistant(SettingsActions, SelfFix, Handoff, CourseChannel, WorkChannel):
    # 明ける時刻が分からないときや、返ってきた時刻が過去だったときに待つ時間
    LIMIT_FALLBACK_SECONDS = 30 * 60
    # 明けた直後に詰まらないよう、少しだけ余分に待つ
    LIMIT_MARGIN_SECONDS = 60

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
        # スレッドごとのロックは捨てずに残す。「待っている依頼がいるか」は release の直後に
        # 一瞬だけ「いない」と見えるので、そこで捨てると、待っていた依頼が別のロックを取り、
        # 同じスレッド（同じセッション）の claude が2本同時に走る
        self.thread_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.channel_names: dict[str, str] = {}
        # この起動で Notion に登録済みのテーマ（招待の取りこぼしを、使うときに埋める）
        self.registered_themes: set[str] = set()
        # 同じテーマで重なって動いたかを覚えておく（outputs/ が共通なので図が混ざる）
        self.theme_runs = ThemeRuns()
        self.tasks: set[asyncio.Task] = set()
        # いま claude が動いている数と、1つも動いていないことを知らせる合図。
        # 自分を入れ替えるときに、作業が終わるのを待つのに使う
        self.running = 0
        self.idle = asyncio.Event()
        self.idle.set()
        # 取り込んだあと、作業がなくなったら終了する（launchd が新しい版で起動し直す）
        self.restart_requested = asyncio.Event()
        # 契約の上限に達した。この時刻までは、決まった時刻の処理も始めない
        self.limited_until = 0.0
        # ほかのエージェント（A2A）。オーケストレーターとして、仕事を頼む相手（docs/agents.md）
        self.agents: dict[str, a2a.Agent] = agents.build(config)
        # 名刺から読んだスキルの一覧（振り分け係が使う）。エージェントを入れ替えるとスキルが増えるので、
        # しばらくたったら読み直す（本体の再起動を待たない）
        self.agent_skills: dict[str, list[dict]] = {}
        self.agent_skills_read_at: dict[str, float] = {}

    @contextmanager
    def claude_running(self):
        """claude が動いている間を数える。0 になったら idle の合図を立てる。"""
        self.running += 1
        self.idle.clear()
        try:
            yield
        finally:
            self.running -= 1
            if self.running == 0:
                self.idle.set()

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """裏で動かす。終わるまで参照を持っておく（持たないと途中で回収されることがある）。"""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def thread_ui(self, req: Request) -> ThreadUI:
        return ThreadUI(self.slack, req.channel, req.thread_ts, self.team_id, self.config.allowed_user_id)

    def is_allowed(self, user: str | None) -> bool:
        return guard.is_owner(self.config, user)

    # Slack の読み書き

    async def channel_name(self, channel: str) -> str:
        """テーマの名前（チャンネル名から、並び順のための番号を外したもの）。"""
        if channel not in self.channel_names:
            info = await self.slack.conversations_info(channel=channel)
            self.channel_names[channel] = themes.theme_name(info["channel"]["name"])
        return self.channel_names[channel]

    async def channel_ids(self) -> dict[str, str]:
        """Kei Agent が参加しているチャンネルの、名前から ID への対応。"""
        ids: dict[str, str] = {}
        cursor = None
        while True:
            resp = await self.slack.conversations_list(
                types="public_channel,private_channel", exclude_archived=True, limit=200, cursor=cursor
            )
            for c in resp.get("channels", []):
                if c.get("is_member"):
                    # 番号つきの名前（`10_amr-query`）でも、テーマ名（`amr-query`）でも引けるようにする
                    ids[c["name"]] = ids[themes.theme_name(c["name"])] = c["id"]
                    self.channel_names[c["id"]] = themes.theme_name(c["name"])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return ids

    async def thread_messages(self, channel: str, thread_ts: str) -> tuple[list[dict], int]:
        """スレッドの投稿を新しいほうから HISTORY_MAX_MESSAGES 件まで。載せきれず落とした件数も返す。"""
        messages: list[dict] = []
        dropped = 0
        cursor = None
        for _ in range(HISTORY_MAX_PAGES):
            resp = await self.slack.conversations_replies(
                channel=channel, ts=thread_ts, limit=HISTORY_PAGE, cursor=cursor)
            messages += resp.get("messages", [])
            if len(messages) > HISTORY_MAX_MESSAGES:
                dropped += len(messages) - HISTORY_MAX_MESSAGES
                messages = messages[-HISTORY_MAX_MESSAGES:]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        else:
            log.warning("スレッド %s の履歴が長すぎるので、途中で読むのをやめました", thread_ts)
        return messages, dropped

    async def fetch_message(self, channel: str, ts: str) -> dict | None:
        resp = await self.slack.conversations_replies(channel=channel, ts=ts, inclusive=True, limit=HISTORY_PAGE)
        return next((m for m in resp.get("messages", []) if m.get("ts") == ts), None)

    async def permalink(self, channel: str, ts: str) -> str:
        resp = await self.slack.chat_getPermalink(channel=channel, message_ts=ts)
        return resp["permalink"]

    async def post(self, req: Request, text: str, markdown: bool = False) -> None:
        if markdown:
            await self.slack.chat_postMessage(channel=req.channel, thread_ts=req.thread_ts, markdown_text=text)
        else:
            await self.slack.chat_postMessage(channel=req.channel, thread_ts=req.thread_ts, text=text)

    async def _react(self, method, channel: str, ts: str, name: str) -> None:
        try:
            await method(channel=channel, timestamp=ts, name=name)
        except Exception:
            log.debug("リアクションを変えられません", exc_info=True)

    async def react_done(self, channel: str, ts: str) -> None:
        await self._react(self.slack.reactions_add, channel, ts, DONE_REACTION)

    async def mark_answered(self, req: Request, failed: bool) -> None:
        """答えた依頼の 👀 を外し、✅（止まったときは ⚠️）をつける。どの依頼に答えたかが一目で分かる。"""
        if not req.message_ts:
            return
        await self._react(self.slack.reactions_remove, req.channel, req.message_ts, SEEN_REACTION)
        await self._react(self.slack.reactions_add, req.channel, req.message_ts,
                          FAILED_REACTION if failed else DONE_REACTION)

    async def notify_owner(self, req: Request, text: str) -> None:
        """スレッド内の投稿は通知が来ないので、依頼者へのメンション付きの短い投稿を足す。"""
        try:
            await self.post(req, f"<@{self.config.allowed_user_id}> {text}")
        except Exception:
            log.warning("依頼者への通知を投稿できません", exc_info=True)

    async def notify_trouble(self, text: str) -> None:
        """うまくいかなかったことを、Kei Agent の改善のチャンネルに知らせる。"""
        log.warning(text)
        try:
            ids = await self.channel_ids()
            channel = next((ids[n] for n in self.config.improve_channels if n in ids), None)
            if channel:
                await self.slack.chat_postMessage(channel=channel, text=f"{FAILED_PREFIX} {text[:2500]}")
        except Exception:
            log.exception("Kei Agent の改善のチャンネルに知らせられません")

    # Slack の出来事

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
        """スレッド内の、メンションなしの返信。Kei Agent が動いているスレッドだけに反応する。"""
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
            text = f"Kei Agent です。このチャンネルでメンションされた要望は `{self.config.backlog_path}` に記録します。"
        elif ws.kind is ChannelKind.OVERVIEW:
            text = f"Kei Agent です。このチャンネルでは、すべてのテーマを読んで相談に乗ります。書き込みは `{ws.cwd}` だけにします。"
        elif ws.kind is ChannelKind.COURSE:
            text = "Kei Agent です。このチャンネルの用事は大学エージェントに取り次ぎます。\n" + course.CAN_DO
        elif ws.kind is ChannelKind.WORK:
            text = "Kei Agent です。このチャンネルの用事は仕事エージェントに取り次ぎます。\n" + work.CAN_DO
        else:
            state = "作りました" if created else "使います"
            text = (
                f"Kei Agent です。このチャンネルのテーマ用に `{ws.cwd}` を{state}。"
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
        settings.drop_theme(self.store, await self.channel_name(channel))

    async def check_agents(self) -> dict[str, list[str]]:
        """つないでいるエージェントの名刺を読んで、生きているかと、何ができるかを見る。"""
        skills: dict[str, list[str]] = {}
        for name, agent in self.agents.items():
            try:
                card = await agent.card()
            except Exception as e:
                await self.notify_trouble(f"{name} のエージェントにつながりません（{agent.base_url}）: "
                                          f"{type(e).__name__}: {e}")
                continue
            self._remember_skills(name, card)
            skills[name] = [s["id"] for s in self.agent_skills[name] if s.get("id")]
            log.info("%s のエージェントにつながりました（%s）: %s", name, card.get("name", "?"),
                     "、".join(skills[name]) or "できることなし")
        return skills

    def _remember_skills(self, name: str, card: dict) -> None:
        self.agent_skills[name] = list(card.get("skills") or [])
        self.agent_skills_read_at[name] = time.time()

    async def skills_of(self, name: str) -> list[dict]:
        """そのエージェントのスキル（名刺から）。古くなっていたら読み直す。"""
        fresh = time.time() - self.agent_skills_read_at.get(name, 0) < SKILLS_TTL_SECONDS
        if not fresh and name in self.agents:
            with suppress(Exception):
                self._remember_skills(name, await self.agents[name].card())
        return self.agent_skills.get(name, [])

    async def check_notion_schema(self) -> list[str]:
        """Notion の項目のずれを起動時に見て、あれば知らせる。黙って定期処理が止まるのを防ぐ。"""
        if self.notion is None:
            return []
        try:
            problems = await asyncio.to_thread(self.notion.schema_problems)
        except Exception as e:
            await self.notify_trouble(f"Notion の設定を確かめられませんでした: {type(e).__name__}: {e}")
            return []
        if problems:
            await self.notify_trouble(
                "Notion の設定が Kei Agent の使う形とずれています。直すまで、Daily や夜間の Task が止まります。\n"
                + "\n".join(f"• {p}" for p in problems))
        return problems

    async def register_theme(self, channel: str, ws: Workspace) -> None:
        if self.notion is None:
            return
        slack_url = f"{self.team_url}archives/{channel}" if self.team_url else ""
        try:
            await asyncio.to_thread(self.notion.ensure_theme, ws.channel_name, slack_url, f"{ws.cwd}/")
        except NotionError as e:
            await self.notify_trouble(f"Notion にテーマ「{ws.channel_name}」を登録できませんでした: {e}")

    def _own_night_reaction(self, event: dict) -> bool:
        """依頼者が自分のメッセージに 🌙 をつけた（外した）か。"""
        item = event.get("item") or {}
        return (event.get("reaction") == NIGHT_REACTION and item.get("type") == "message"
                and self.is_allowed(event.get("user")) and event.get("item_user") == event.get("user"))

    async def on_reaction_added(self, event: dict) -> None:
        """自分のメッセージに 🌙 をつけると、夜間の Task になる。"""
        if not self._own_night_reaction(event):
            return
        channel, ts = event["item"]["channel"], event["item"]["ts"]
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
            await self.post(req, f"{FAILED_PREFIX} Notion に Task を作れなかったよ")
            await self.notify_trouble(f"🌙 の Task を Notion に作れませんでした: {e}")
            return
        await self.post(req, f"🌙 今夜の Task にしたよ: <{task.url}|{task.title}>")

    async def on_reaction_removed(self, event: dict) -> None:
        if not self._own_night_reaction(event) or self.notion is None:
            return
        link = await self.permalink(event["item"]["channel"], event["item"]["ts"])
        try:
            await asyncio.to_thread(self.notion.cancel_night_task, link)
        except NotionError as e:
            await self.notify_trouble(f"🌙 を外した Task を Notion で取り消せませんでした: {e}")

    async def run_claude(self, ws: Workspace, prompt: str, session_id: str | None = None,
                         channel: str = "", thread_ts: str = "",
                         on_activity=None, on_text=None) -> runner.RunResult:
        """claude を1回動かす。研究エージェント（A2A）が設定されていれば、そちらに頼む。

        どちらで動かしても、同じ `config.toml` の柵（sandbox、読ませない場所、接続先）で動く。
        """
        agent = self.agents.get(research.AGENT)
        with self.claude_running():
            if agent is not None:
                return await research.run(agent, ws, prompt, session_id, channel, thread_ts,
                                          on_activity, on_text)
            return await runner.run_claude(self.config, ws, prompt, session_id, channel, thread_ts,
                                           on_activity, on_text)

    # 決まった時刻の処理から使う（schedule.py）

    async def run_detached(self, ws: Workspace, channel_name: str, prompt: str, trigger: str) -> runner.RunResult:
        """スレッドを作らずに claude -p を動かす（定期処理用）。結果を見てから投稿先を決める。"""
        assert ws.cwd is not None
        themes.ensure_workspace(ws)
        async with self.semaphore:
            run_id = self.store.start_run("", "", channel_name, trigger)
            result = await self.run_claude(ws, prompt)
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
            append_thread_log(ws.cwd, channel_name, thread_ts, "Kei Agent", result.text)
            for chunk in split_text(result.text):
                await self.post(req, chunk, markdown=True)
        if result.is_error:
            await self.post(req, f"{FAILED_PREFIX} エラーで止まっちゃった: {'; '.join(result.errors)[:1500] or '原因不明'}")
        if footer:
            await self.post(req, footer)
        return thread_ts

    # 依頼の処理

    async def submit(self, req: Request) -> None:
        """依頼を受け付けて、裏で処理を始める。"""
        if req.trigger == "message":
            self.store.set_awaiting(req.channel, req.thread_ts, False)
        if req.trigger in ("message", "voice"):
            await self.drop_deferred_for(req)
        if req.message_ts:
            await self._react(self.slack.reactions_add, req.channel, req.message_ts, SEEN_REACTION)
        self.spawn(self._process_and_report(req))

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
        if await self.tell_if_waiting(req):
            return None
        if ws.kind is ChannelKind.IMPROVE:
            return await self.improve(req, ws)
        if ws.kind in (ChannelKind.COURSE, ChannelKind.WORK):
            # 同じスレッドで2つ同時に動かさない。claude を動かす仕事もあるので、全体の上限も守る
            async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
                await (self.course(req) if ws.kind is ChannelKind.COURSE else self.work(req))
            return None
        themes.ensure_workspace(ws)
        if ws.kind is ChannelKind.THEME:
            ws = replace(ws, allowed_domains=tuple(settings.theme_domains(self.store, ws.channel_name)))
            if ws.channel_name not in self.registered_themes:
                # 招待のイベントを取りこぼしていても、1テーマ = 1チャンネル = 1ディレクトリ = Notion の1行を保つ
                self.registered_themes.add(ws.channel_name)
                await self.register_theme(req.channel, ws)
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
            try:
                return await self.run(req, ws)
            except Exception as e:
                log.exception("依頼の処理に失敗しました")
                await self.post(req, f"{FAILED_PREFIX} 内部エラーで止まっちゃった: `{type(e).__name__}: {e}`")
                return None

    async def drop_deferred_for(self, req: Request) -> None:
        """上限で止まって自動でやり直す予定だった依頼を、このスレッドのぶんだけ取り消す。

        依頼者が「続けて」と書いたあとに、同じ依頼が裏でもう一度走ると、二重に作業してしまう。
        """
        canceled = [i for i, payload in self.store.pending_deferred("request")
                    if payload.get("channel") == req.channel and payload.get("thread_ts") == req.thread_ts]
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

    async def run(self, req: Request, ws: Workspace) -> runner.RunResult:
        """1回分の依頼を claude に渡し、結果をスレッドに返す。スレッドのロックを取ってから呼ぶ。"""
        assert ws.cwd is not None
        saved = await download_files(req.files, ws.cwd, self.bot_token)
        prompt = req.text
        if saved:
            prompt += "\n\n添付ファイル（保存先）:\n" + "\n".join(f"- {p}" for p in saved)
        append_thread_log(ws.cwd, req.channel_name, req.thread_ts, WHO_BY_TRIGGER.get(req.trigger, "依頼者"),
                          req.text + ("\n\n" + "\n".join(f"- 添付: `{p}`" for p in saved) if saved else ""))

        started = time.monotonic()
        ui = self.thread_ui(req)
        await ui.start()
        self.theme_runs.begin(req.channel_name, req.thread_ts)
        before = snapshot_outputs(ws.cwd)
        run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)
        # 途中で終了させられても、次の起動で拾ってやり直せるように控えておく
        in_flight = self.store.start_in_flight(req.to_payload())
        try:
            result = await self._converse(req, ws, prompt, ui)
        finally:
            self.store.finish_deferred(in_flight)
        self.store.end_run(run_id, result.is_error, result.cost_usd)
        if req.trigger in ("message", "voice"):
            self.store.count_turn(req.channel, req.thread_ts)
        self.store.set_stalled(req.channel, req.thread_ts, req.text if result.is_error else None)

        if result.limit_reset_at is not None:
            await self.defer_for_limit(req, result.limit_reset_at)
            self.store.set_awaiting(req.channel, req.thread_ts, True)
            await ui.finish(result.text, awaiting=True)
            await self.mark_answered(req, failed=True)
            self.theme_runs.end(req.channel_name, req.thread_ts)
            return result

        connect = self.new_connect_requests(ws, result.text, result.requested_domains)
        awaiting = req.awaiting_after or result.is_error or AWAITING_MARKER in result.text or bool(connect)
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        await self.sync_review_conclusion(req)
        await self._reply(req, ws, ui, result, awaiting)
        await self._attach_outputs(req, ws.cwd, before)
        await self.mark_answered(req, result.is_error)
        await self.ask_for_domains(req, ws, connect)
        await self.handle_job_requests(ws.cwd)
        waiting_for_job = any(j.channel == req.channel and j.thread_ts == req.thread_ts
                              for j in self.store.active_jobs())
        offer, title = self.should_offer_handoff(req, ws, result.text, busy=awaiting or waiting_for_job)
        if offer and not result.is_error:
            await self.offer_handoff(req, title)
        if awaiting:
            await self.notify_owner(req, "返事がほしいよ")
        elif not waiting_for_job and time.monotonic() - started >= NOTIFY_AFTER_SECONDS:
            await self.notify_owner(req, "終わったよ")
        if waiting_for_job:
            await ui.keep_working()
        return result

    async def _converse(self, req: Request, ws: Workspace, prompt: str, ui: ThreadUI | None) -> runner.RunResult:
        """このスレッドの会話の続きとして claude を動かす。会話が失われていたら、Slack の履歴から戻す。

        ui がなければ、経過を Slack に見せずに動かす（引き継ぎメモを書かせるときなど）。
        """
        # ジョブの依頼をこのスレッドのものとして確かめられるよう、先にスレッドを記録する
        row = self.store.get_thread(req.channel, req.thread_ts)
        self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, None)
        session_id = row["session_id"] if row else None
        version = runner.system_prompt_version(self.config)
        if session_id and row["prompt_version"] != version:
            # --resume では会話を始めたときのシステムプロンプトが使われ続けるので、変わった決まりを本文で渡す
            prompt = rules_update_prompt(runner.system_prompt_text(self.config)) + prompt
        # 区切って立てたスレッドの最初の回には、前のスレッドの引き継ぎメモを渡す
        prompt = self.handoff_memo_for(row) + prompt
        stalled = row["stalled_request"] if row else None
        if stalled:
            # 止まった回は claude 側に記録が残らないことがあるので、resume せず Slack の履歴から文脈を戻す
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            prompt = history_prompt(messages, self.bot_user_id, prompt, req.message_ts,
                                    stalled if stalled != req.text else None, dropped=dropped)
            session_id = None

        on_activity, on_text = (ui.activity, ui.text) if ui is not None else (None, None)

        async def attempt(prompt: str, session_id: str | None) -> runner.RunResult:
            return await self.run_claude(ws, prompt, session_id, req.channel, req.thread_ts,
                                         on_activity, on_text)

        result = await attempt(prompt, session_id)
        if result.session_missing:
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            result = await attempt(
                history_prompt(messages, self.bot_user_id, prompt, req.message_ts, dropped=dropped), None)
        if result.session_id:
            self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, result.session_id)
            self.store.set_prompt_version(req.channel, req.thread_ts, version)
        return result

    async def _reply(self, req: Request, ws: Workspace, ui: ThreadUI, result: runner.RunResult,
                     awaiting: bool) -> None:
        """まとめをスレッドに返す。流して見せられなかったときだけ、まとめて投稿する。"""
        assert ws.cwd is not None
        # 着手・取り込みの合図は、検出に使うだけで Slack には出さない（result.text は残す）
        # 区切りの合図は、題をボタンに出すので本文からは消す
        shown = improve.strip_markers(result.text) if ws.kind is ChannelKind.IMPROVE else strip_handoff(result.text)
        streamed = await ui.finish(shown, awaiting and not result.is_error)
        if shown:
            append_thread_log(ws.cwd, req.channel_name, req.thread_ts, "Kei Agent", shown)
            if not streamed:
                for chunk in split_text(shown):
                    await self.post(req, chunk, markdown=True)
        if result.is_error:
            reason = "上限時間を超えたので止めました" if result.timed_out else "; ".join(result.errors)[:1500]
            await self.post(req, f"{FAILED_PREFIX} エラーで止まっちゃった: {reason or '原因不明'}")

    async def _attach_outputs(self, req: Request, cwd: Path, before) -> None:
        """この回で新しくできた outputs/ のファイルをスレッドに添付する。"""
        overlapped = self.theme_runs.end(req.channel_name, req.thread_ts)
        notes = []
        try:
            new_files = changed_files(before, snapshot_outputs(cwd), req.outputs_since)
            await self.upload_outputs(req, cwd, new_files)
            if new_files and overlapped:
                # 時刻では自分の図と隣の図を区別できないので、混ざりうることを黙って隠さない
                notes.append("このテーマで別のスレッドも動いていたので、別のスレッドの図が混ざっているかもしれない。")
        except Exception as e:
            # 結果はもう返しているので、添付だけ失敗したことを伝える
            log.exception("outputs/ のファイルを添付できません")
            notes.append(f"`outputs/` のファイルを添付できなかったよ: `{type(e).__name__}: {e}`")
        if notes:
            await self.post(req, f"{FAILED_PREFIX} " + " ".join(notes))

    async def upload_outputs(self, req: Request, cwd: Path, files: list[Path]) -> None:
        uploadable, skipped = split_uploads(files)
        if uploadable:
            await self.slack.files_upload_v2(
                channel=req.channel,
                thread_ts=req.thread_ts,
                file_uploads=[{"file": str(p), "filename": p.name, "title": str(p.relative_to(cwd))} for p in uploadable],
            )
        if skipped:
            names = "\n".join(f"• `{p.relative_to(cwd)}`" for p in skipped)
            await self.post(req, f"添付しなかったファイル（数か大きさの上限を超えたもの）:\n{names}")

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

    # Slack の外からの依頼（声のレイヤなど。docs/plan.md の13章）

    async def ask_loop(self) -> None:
        """同じ Mac に置かれた依頼を、数秒ごとに拾う。"""
        failing = False
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
        pending = ask.pending_asks(self.config)
        if not pending:
            return
        ids = await self.channel_ids()
        for path, payload in pending:
            path.unlink(missing_ok=True)
            theme = str(payload.get("theme") or "")
            text = str(payload.get("text") or "").strip()
            kind = payload.get("kind", "request")
            channel = ids.get(theme)
            if not channel or not text:
                await self.notify_trouble(
                    f"外からの依頼を渡せませんでした（テーマ: {theme or '不明'}）: {text[:100] or '（空）'}")
                continue
            header = "📌 声で決まったこと" if kind == "note" else "🎤 声からの依頼"
            resp = await self.slack.chat_postMessage(channel=channel, text=f"{header}\n{text}")
            if kind == "note":
                continue
            await self.submit(Request(
                channel=channel, channel_name=theme, thread_ts=resp["ts"], message_ts=None,
                text=text, trigger="voice",
            ))

    # 契約の上限（Claude AI usage limit）

    def limit_until(self, reset_at: float, now: float | None = None) -> float:
        """いつやり直すか。明ける時刻が古いまま返ることがあるので、過去ならしばらく待つ。"""
        now = time.time() if now is None else now
        if reset_at <= now:
            return now + self.LIMIT_FALLBACK_SECONDS
        return reset_at + self.LIMIT_MARGIN_SECONDS

    async def note_limit(self, reply: agents.Reply) -> None:
        """エージェントが上限に当たったことを、本体の1か所に集める（約束は本体が持つ）。

        エージェントは自分の claude を動かすが、契約の枠は1つなので、待つ・やり直すの管理は
        オーケストレーターに寄せる（docs/agents.md）。
        """
        if reply.limit_reset_at is None:
            return
        until = self.limit_until(reply.limit_reset_at)
        if until <= self.limited_until:
            return
        self.limited_until = until
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        await self.notify_trouble(f"エージェントが Claude の契約の上限に当たりました。{when} ごろまで待ちます。")

    async def defer_for_limit(self, req: Request, reset_at: float) -> None:
        """上限に達した依頼を、明けてからやり直すものとして覚えておく。"""
        until = self.limit_until(reset_at)
        self.limited_until = max(self.limited_until, until)
        self.store.defer_run("request", req.to_payload(), until)
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        await self.post(req, f"{FAILED_PREFIX} Claude の契約の上限に達したみたい。{when} ごろに自動でやり直すね。")

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
            try:
                await self.post(req, f"{FAILED_PREFIX} さっきの作業は Kei Agent の入れ替えで途中で止まっちゃった。"
                                     "いまの状態を確かめて、続きからやり直すね。")
            except Exception:
                log.warning("中断を知らせられません", exc_info=True)
            # 元のメッセージの 👀 は残したまま。やり直しが終われば ✅ か ⚠️ に変わる
            await self.submit(replace(req, text=interrupted_prompt(req.text)))
        if interrupted:
            log.info("再起動で止まっていた依頼を %d 件やり直します", len(interrupted))
        return len(interrupted)

    async def retry_deferred(self, now: float | None = None) -> None:
        """上限で止まった依頼を、明けたらやり直す。"""
        now = time.time() if now is None else now
        for deferred_id, payload in self.store.due_deferred("request", now):
            self.store.finish_deferred(deferred_id)
            await self.submit(Request.from_payload(payload))

    # ジョブ

    async def handle_job_requests(self, cwd: Path) -> None:
        for o in await self.jobs.process_requests(cwd):
            # 依頼のチャンネルとスレッドは Claude が書いたものなので、知っているスレッドのときだけ投稿する
            known = bool(o.channel and o.thread_ts and self.store.get_thread(o.channel, o.thread_ts))
            req = Request(o.channel, "", o.thread_ts, None, "")
            if o.error:
                text = (f"ジョブ「{o.job.name}」を投入できなかったよ: {o.error}") if o.job else o.error
                if known:
                    await self.post(req, f"{FAILED_PREFIX} {text}")
                else:
                    await self.notify_trouble(f"`{cwd}` のジョブの依頼: {text}")
                continue
            await self.post(req, f"🧪 ジョブ {o.job.id}「{o.job.name}」を投入したよ: `{o.job.command}`")

    async def poll_jobs(self) -> None:
        """テーマのディレクトリに残った依頼を処理し、終わったジョブを報告する。"""
        root = self.config.research_root
        if root.is_dir():
            for cwd in sorted(p for p in root.iterdir() if (p / ".kei-agent" / "requests").is_dir()):
                await self.handle_job_requests(cwd)
        for job in await self.jobs.refresh():
            self.jobs.mark_reported(job)
            row = self.store.get_thread(job.channel, job.thread_ts)
            if row is None:
                continue
            req = Request(job.channel, row["channel_name"], job.thread_ts, None, "")
            missing = missing_outputs(job)
            note = f"。ただ {'、'.join(missing)} ができていない" if missing else ""
            await self.post(req, f"🧪 ジョブ {job.id}「{job.name}」が終わったよ"
                                 f"（{job_status_label(job.status)}{note}）。結果を見てみるね")
            await self.submit(replace(
                req, text=job_resume_prompt(job), trigger="job",
                outputs_since=job.submitted_at, awaiting_after=job.status != "succeeded" or bool(missing),
            ))

    async def job_loop(self) -> None:
        failing = False
        while True:
            try:
                await self.poll_jobs()
                await self.retry_deferred()
                failing = False
            except Exception as e:
                log.exception("ジョブの確認に失敗しました")
                if not failing:
                    # 失敗が続いている間は、最初の1回だけ知らせる
                    await self.notify_trouble(f"ジョブの状態を確認できません（pueue が止まっていませんか）: {type(e).__name__}: {e}")
                failing = True
            await asyncio.sleep(self.config.job_poll_seconds)
