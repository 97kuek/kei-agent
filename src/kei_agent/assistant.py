"""Slack の出来事を受けて、claude -p とジョブを動かし、スレッドに返す。

Slack Bolt に依存しないようにし、Slack API は `slack`（AsyncWebClient と同じメソッドを持つもの）として受け取る。
役割ごとの処理は、次のファイルに分けて Assistant に混ぜている。

- settings_actions.py: 接続先の申し出のボタンと App Home
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
    api,
    ask,
    guard,
    modules,
    router,
    runner,
    settings,
    themes,
    updates,
    version,
)
from kei_agent.auto_messages import (
    history_prompt,
    interrupted_prompt,
    job_resume_prompt,
    job_status_label,
    today_line,
)
from kei_agent.config import Config
from kei_agent.execution_contract import prompt_version
from kei_agent.handoff import Handoff, strip_handoff
from kei_agent.home import agent_labels
from kei_agent.jobs import JobManager, missing_outputs
from kei_agent.model_policy import (
    PROVIDERS,
    ModelPolicyError,
    UseCase,
    explicit_use_case,
    is_manual,
    resolve,
    resolve_selected,
)
from kei_agent.notion import NotionError
from kei_agent.notion_hub import HubStore
from kei_agent.notion_store import NotionStore
from kei_agent.request import Request
from kei_agent.response_output import (
    OutputError,
    finalize_conversation,
    safe_failure,
    trouble_notice,
    validate_daily,
    validate_review,
)
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
    strip_lines,
)
from kei_agent.store import Store
from kei_agent.theme_files import (
    append_thread_log,
    changed_files,
    download_files,
    snapshot_outputs,
    split_uploads,
)
from kei_agent.theme_invite import ThemeInvite
from kei_agent.themes import ChannelKind, Workspace
from kei_agent.thread_ui import ThreadUI

log = logging.getLogger(__name__)

# これ以上かかった作業が終わったら、依頼者に通知の別投稿を送る（短い依頼には送らない）
NOTIFY_AFTER_SECONDS = 60
# 名刺（エージェントのスキル）を読み直す間隔。入れ替えても、これだけたてば新しいスキルを使える
SKILLS_TTL_SECONDS = 600
# 古い版の担当を起動し直してから、名刺を読み直すまでの秒数
STALE_RECHECK_SECONDS = 20
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



# run_agent が provider 未選択で止めたときの印（render_reply が案内文に変える）
NO_PROVIDER = "provider が選ばれていません"
# 声からの問い合わせの用途（読むだけ。軽い recipe で答える。モジュールの担当は module.toml の default_use_case）
# Kei Agent のチャンネルの会話を受け持つモジュール（自己改善）が、オンになっていないとき
NO_IMPROVE_OWNER = ("このチャンネルでは、Kei Agent で確認が必要なことを知らせるだけだよ。要望を聞いて直すには、"
                    "config.toml の modules に improve を足してね。")
# 研究テーマ（ほかのどれにも当たらないチャンネル）を受け持つモジュールが、オンになっていないとき
NO_THEME_OWNER = ("このチャンネルを受け持つモジュールがないよ。研究テーマに使うなら、config.toml の modules に"
                  " research を足してね。")
# 声からの問い合わせで使う、読むだけの軽い用途（研究のモジュールの用途。無ければ担当の default_use_case）
VOICE_USE_CASES = {"research": "research_extract"}


class Assistant(SettingsActions, Handoff, ThemeInvite):
    # 明ける時刻が分からないときや、返ってきた時刻が過去だったときに待つ時間
    LIMIT_FALLBACK_SECONDS = 30 * 60
    # 明けた直後に詰まらないよう、少しだけ余分に待つ
    LIMIT_MARGIN_SECONDS = 60

    def __init__(self, config: Config, store: Store, slack, jobs: JobManager, bot_token: str, bot_user_id: str,
                 notion: NotionStore | None = None, team_url: str = "", team_id: str = "",
                 hub: HubStore | None = None):
        self.config = config
        self.notion = notion
        self.hub = hub
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
        # この起動が、入れ替えたあとのものなら、その結果（Slack につながったあとに take_update で読む）
        self.last_update: updates.Update | None = None
        # 契約の上限に達した。この時刻までは、決まった時刻の処理も始めない
        # ほかのエージェント（A2A）。オーケストレーターとして、仕事を頼む相手（docs/architecture.md の「振り分けと A2A」）
        self.agents: dict[str, a2a.Agent] = agents.build(config)
        # 名刺から読んだスキルの一覧（振り分け係が使う）。エージェントを入れ替えるとスキルが増えるので、
        # しばらくたったら読み直す（本体の再起動を待たない）
        self.agent_skills: dict[str, list[dict]] = {}
        self.agent_skills_read_at: dict[str, float] = {}
        # モジュールの窓口（kei_agent.api.Core）と、動き（modules/<名前>/module.py の class Module）。
        # コアとモジュールは窓口でだけやり取りする
        # 研究テーマを受け持つモジュールが core.work で答えた結果（process が呼び出し元に返す。依頼ごと）
        self._work_results: dict[int, runner.RunResult] = {}
        self.cores = {spec.name: api.Core(self, spec) for spec in modules.enabled(config.modules)}
        self.modules: dict[str, object] = {
            spec.name: cls(self.cores[spec.name])
            for spec in modules.enabled(config.modules) if (cls := modules.load_code(spec)) is not None}

    def take_update(self) -> updates.Update | None:
        """Slack につながったあとに呼ぶ。入れ替えたあとの起動なら、その結果を覚えて、印を消す（deploy/run.sh が戻さない）。"""
        self.last_update = updates.take_update(self.config)
        return self.last_update

    def restart_for_update(self, previous: str, note: str = "") -> None:
        """新しい版で起動し直す。起動できなければ deploy/run.sh が previous に戻す（updates.py）。"""
        updates.mark_pending(self.config, previous, note)
        self.request_restart()

    def request_restart(self) -> None:
        """動いている AI の作業がなくなったら終了する（launchd が新しい版で起動し直す）。"""
        async def wait_then_restart() -> None:
            await self.idle.wait()
            # エージェントも同じリポジトリを読むので、一緒に入れ替える（本体だけだと古いまま動く）
            await asyncio.to_thread(updates.restart_agents)
            log.info("新しい版で起動し直すため、終了します")
            self.restart_requested.set()

        self.spawn(wait_then_restart())

    async def modules_started(self) -> None:
        """起動して Slack につながったあと（class Module の on_start）。1つが落ちても、ほかは続ける。"""
        for name, module in self.modules.items():
            on_start = getattr(module, "on_start", None)
            if on_start is None:
                continue
            try:
                await on_start()
            except Exception:
                log.exception("モジュール「%s」の起動のときの処理が落ちました", name)
                await self.notify_trouble(f"モジュール「{name}」の起動のときの処理が落ちました")

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
        task.add_done_callback(self._log_if_broken)
        return task

    @staticmethod
    def _log_if_broken(task: asyncio.Task) -> None:
        """裏で動かした仕事が落ちたら、必ずログに残す。

        これがないと、例外が誰にも見られないまま消える（引き継ぎが
        「新しいスレッドに引き継いでいるよ…」のまま止まったのはこれ）。
        """
        if task.cancelled():
            return
        if (error := task.exception()) is not None:
            log.error("裏で動かした仕事が落ちました", exc_info=error)

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
        self.emit("failed" if failed else "done", theme=req.channel_name)

    async def notify_owner(self, req: Request, text: str) -> None:
        """スレッド内の投稿は通知が来ないので、依頼者へのメンション付きの短い投稿を足す。"""
        try:
            await self.post(req, f"<@{self.config.allowed_user_id}> {text}")
        except Exception:
            log.warning("依頼者への通知を投稿できません", exc_info=True)

    async def notify_trouble(self, text: str) -> None:
        """詳細はログに残し、改善チャンネルには何が起きたかを1行で知らせる（パスは名前だけ、長さは切る）。"""
        log.warning(text)
        try:
            ids = await self.channel_ids()
            channel = next((ids[n] for n in self.config.improve_channels if n in ids), None)
            if channel:
                await self.slack.chat_postMessage(
                    channel=channel,
                    text=f"{FAILED_PREFIX} Kei Agent で確認が必要な問題が起きたよ: {trouble_notice(text)}")
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
        if ws.kind is ChannelKind.THEME and not ws.external and ws.cwd is not None and not ws.cwd.exists():
            # まだフォルダの無い研究テーマ: 置き場所を聞く（既定の場所に作る／既存のフォルダを使う。theme_invite.py）
            await self.ask_theme_place(channel, ws)
            return
        created = themes.ensure_workspace(ws)
        self.registered_themes.add(name)
        if ws.kind is ChannelKind.IMPROVE:
            # Kei Agent のチャンネル。会話は、受け持つモジュール（core_channels。自己改善）があれば、そのモジュール
            text = "Kei Agent です。このチャンネルには、Kei Agent で確認が必要なことが起きたときに知らせます。"
            welcome = getattr(self.modules.get(ws.module), "welcome", None) if ws.module else None
            if welcome is not None:
                text += "\n" + welcome()
        elif ws.kind is ChannelKind.OVERVIEW:
            text = f"Kei Agent です。このチャンネルでは、すべてのテーマを読んで相談に乗ります。書き込みは `{ws.cwd}` だけにします。"
        elif ws.kind is ChannelKind.MODULE:
            text = f"Kei Agent です。このチャンネルの用事は{modules.known()[ws.module].label}エージェントに取り次ぎます。"
            welcome = getattr(self.modules.get(ws.module), "welcome", None)
            if welcome is not None:
                text += "\n" + welcome()
        else:
            state = "作りました" if created else "使います"
            text = (
                f"Kei Agent です。このチャンネルのテーマ用に `{ws.cwd}` を{state}。"
                "研究の前提を `CLAUDE.md` に書いておくと、依頼のたびに説明しなくて済みます。"
            )
            welcome = getattr(self.modules.get(ws.module), "welcome", None) if ws.module else None
            if welcome is not None:
                # テーマを受け持つモジュールの案内
                text += "\n" + welcome()
            # 研究ホームのテーマの行（研究ホームは Notion のモジュールができるまで本体が持つ）
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
        """つないでいるエージェントの名刺を読んで、生きているか、何ができるか、本体と同じ版かを見る。

        古い版のまま動いている担当は起動し直す（手作業のデプロイで担当だけ起動し直し忘れると、古いコードが
        新しい設定を読めずに止まる。2026-09-26 に大学の担当で起きた）。
        """
        skills: dict[str, list[str]] = {}
        stale: list[str] = []
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
            if version.differs(str(card.get("version") or "")):
                log.warning("%s の担当が古い版のまま動いています（本体 %s、担当 %s）", name, version.RUNNING,
                            card.get("version"))
                stale.append(name)
        if stale:
            self.spawn(self._restart_stale_agents(stale))
        return skills

    async def _restart_stale_agents(self, names: list[str]) -> None:
        """古い版の担当を起動し直し、少し待って確かめる。それでも古ければ知らせる。"""
        for name in names:
            await asyncio.to_thread(updates.restart_service, name)
        await asyncio.sleep(STALE_RECHECK_SECONDS)
        for name in names:
            try:
                theirs = str((await self.agents[name].card()).get("version") or "")
            except Exception:
                theirs = ""
            if theirs != version.RUNNING:
                await self.notify_trouble(f"{name} の担当が古い版のまま動いています。"
                                          "deploy/restart-all.sh で起動し直してください")

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
        except Exception:
            log.exception("Notion の設定を確かめられませんでした")
            await self.notify_trouble("Notion の設定を確かめられませんでした")
            return []
        if problems:
            await self.notify_trouble(
                "研究 Notion の設定が Kei Agent の使う形とずれています。夜間の Task などが止まります。\n"
                + "\n".join(f"• {p}" for p in problems))
        return problems

    async def check_hub_schema(self) -> list[str]:
        """共通ホームの問題は知らせるが、他の agent や研究 Notion の起動を止めない。"""
        if self.hub is None:
            await self.notify_trouble(
                "共通 Notion ホームを利用できません。親ページの共有と hub state を確認してください。"
                "Daily とレトプラは Slack にだけ出し、時間の記録は Notion への送信を保留します")
            return ["共通 Notion ホームを利用できません"]
        try:
            problems = await asyncio.to_thread(self.hub.schema_problems)
        except Exception:
            log.exception("共通 Notion ホームの設定を確かめられませんでした")
            self.hub = None
            await self.notify_trouble("共通 Notion ホームの設定を確認できません。Daily とレトプラは Notion に保存できません")
            return ["共通 Notion ホームの確認に失敗しました"]
        if problems:
            self.hub = None
            await self.notify_trouble("共通 Notion ホームの項目を確認してください:\n"
                                      + "\n".join(f"• {p}" for p in problems))
        elif not self.hub.has_time_db:
            await self.notify_trouble(
                "共通 Notion ホームに「時間記録」がまだありません。kei-agent-hub-setup --apply で作って再起動してください"
                "（それまで時間は Toggl にだけ送り、Notion への送信は保留します）")
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

    def _usable(self, name: str) -> bool:
        """そのモジュールを動かせるか（担当プロセスを持つなら、その住所がある。使っていない担当は黙って飛ばす）。"""
        return modules.known()[name].port is None or name in self.agents

    async def module_prepare(self, kind: str, day: str) -> list[str]:
        """Daily・振り返りの前の取り込み（class Module の prepare）。うまくいかなかったことの短い名前を返す。"""
        failed: list[str] = []
        for name, module in self.modules.items():
            prepare = getattr(module, "prepare", None)
            if prepare is None or not self._usable(name):
                continue
            try:
                failed += [str(label) for label in await prepare(kind, day) or []]
            except Exception:
                log.exception("モジュール「%s」の取り込みが落ちました", name)
                failed.append(f"{modules.known()[name].label}の取り込み")
        return failed

    async def module_agenda(self, days: int, kinds: frozenset[str] | None = None,
                            ) -> tuple[dict[str, list[dict]], list[str]]:
        """モジュールの予定（class Module の agenda）。読めたモジュールの名前 → 予定と、読めなかったモジュールの表示名。

        kinds を渡すと、その種類（meeting / class / due）だけを頼む（振り返りの締切のために、会議を AI で読まない）。
        読めなかった（None を返した・落ちた）モジュールは、予定が無いのとは分ける（予定カレンダーの行を
        「要確認」にしないため）。
        """
        found: dict[str, list[dict]] = {}
        failed: list[str] = []
        for name, module in self.modules.items():
            agenda = getattr(module, "agenda", None)
            if agenda is None or not self._usable(name):
                continue
            try:
                items = await agenda(days, kinds)
            except Exception:
                log.exception("モジュール「%s」の予定を読めませんでした", name)
                items = None
            if items is None:
                failed.append(modules.known()[name].label)
                continue
            found[name] = [item for item in items if isinstance(item, dict)
                           and (kinds is None or item.get("kind", "meeting") in kinds)]
        return found, failed

    async def module_reaction(self, event: dict, added: bool) -> bool:
        """モジュールの投稿へのリアクション（朝の読みものへの 👍 など）。どれかのモジュールが扱ったら True。"""
        for name, module in self.modules.items():
            on_reaction = getattr(module, "on_reaction", None)
            if on_reaction is None:
                continue
            try:
                if await on_reaction(event, added):
                    return True
            except Exception:
                # 1つのモジュールが落ちても、ほかのモジュールと 🌙 は止めない
                log.exception("モジュール「%s」がリアクションを扱えませんでした", name)
                await self.notify_trouble(f"モジュール「{name}」がリアクションを扱えませんでした")
        return False

    def emit(self, kind: str, **fields) -> None:
        """出来事を配る（受け取るのは on_event を持つモジュール。声なら喋る）。投げっぱなしで、届かなくても
        呼んだ側は気にしない。空の中身（None と空文字）は外して渡す。
        """
        data = {key: value for key, value in fields.items() if value not in (None, "")}
        for name, module in self.modules.items():
            on_event = getattr(module, "on_event", None)
            if callable(on_event):
                self.spawn(self._deliver_event(name, on_event, kind, dict(data)))

    @staticmethod
    async def _deliver_event(name: str, on_event, kind: str, data: dict) -> None:
        try:
            await on_event(kind, data)
        except Exception:
            # 知らせを受け取れなかっただけで、配った側の仕事は終わっている
            log.exception("モジュール「%s」が出来事（%s）を受け取れませんでした", name, kind)

    def module_home(self) -> list[tuple[str, list[dict]]]:
        """App Home に並べる、モジュールの項目（class Module の home）。作れなかったモジュールは飛ばす。"""
        sections = []
        for name, module in self.modules.items():
            build = getattr(module, "home", None)
            if not callable(build):
                continue
            try:
                blocks = [block for block in build() or [] if isinstance(block, dict)]
            except Exception:
                log.exception("モジュール「%s」の App Home の項目を作れませんでした", name)
                continue
            if blocks:
                sections.append((modules.known()[name].label, blocks))
        return sections

    async def module_home_action(self, module: str, name: str, action: dict) -> bool:
        """App Home のモジュールの項目が押された（class Module の on_home_action）。扱ったら True。"""
        on_home_action = getattr(self.modules.get(module), "on_home_action", None)
        if not callable(on_home_action):
            return False
        try:
            await on_home_action(name, action)
        except Exception:
            log.exception("モジュール「%s」が App Home の操作（%s）を扱えませんでした", module, name)
            await self.notify_trouble(f"モジュール「{module}」が App Home の操作を扱えませんでした")
        return True

    def _module_target(self, value: str) -> tuple[object, str] | None:
        """モジュールの action_id / callback_id（api.MODULE_PREFIX）から、そのモジュールと名前。"""
        if not value.startswith(api.MODULE_PREFIX):
            return None
        module, _, name = value.removeprefix(api.MODULE_PREFIX).partition(":")
        found = self.modules.get(module)
        return (found, name) if found is not None and name else None

    async def module_action(self, body: dict) -> None:
        """モジュールの投稿のボタンなどが押された（class Module の on_action）。押せるのは依頼者だけ。"""
        if not self.is_allowed(body.get("user", {}).get("id")):
            return
        action = (body.get("actions") or [{}])[0]
        target = self._module_target(str(action.get("action_id") or ""))
        on_action = getattr(target[0], "on_action", None) if target else None
        if callable(on_action):
            await on_action(target[1], body)

    async def module_view(self, body: dict) -> dict | None:
        """モジュールの入力の画面が送られた（class Module の on_view）。欄の下に出す理由を返すと、画面は閉じない。"""
        view = body.get("view") or {}
        target = self._module_target(str(view.get("callback_id") or ""))
        on_view = getattr(target[0], "on_view", None) if target else None
        if not callable(on_view):
            return None
        if not self.is_allowed(body.get("user", {}).get("id")):
            first = next(iter(view.get("blocks") or [{}]), {}).get("block_id", "")
            return {first: "依頼者だけが使えます"} if first else None
        return await on_view(target[1], body)

    async def module_slash(self, name: str, body: dict) -> str:
        """モジュールのスラッシュコマンド（class Module の on_slash_command）。打った人にだけ見せる文を返す。"""
        if not self.is_allowed(str(body.get("user_id") or "")):
            return "この操作は利用できません"
        for module_name, spec in ((n, modules.known()[n]) for n in self.modules):
            if name in spec.slash_commands:
                return str(await self.modules[module_name].on_slash_command(name, body) or "")
        return "このコマンドを受け持つモジュールがありません"

    async def module_material(self, now: float) -> list[str]:
        """Daily と振り返りの材料に、モジュールが足す行（class Module の material）。作れなかったモジュールは飛ばす。"""
        lines: list[str] = []
        for name, module in self.modules.items():
            material = getattr(module, "material", None)
            if not callable(material):
                continue
            try:
                lines += [str(line) for line in await material(now) or []]
            except Exception:
                log.exception("モジュール「%s」の材料を作れませんでした", name)
        return lines

    async def on_reaction_added(self, event: dict) -> None:
        """自分のメッセージに 🌙 をつけると、夜間の Task になる。モジュールの投稿へのリアクションは、そのモジュールが扱う。"""
        if await self.module_reaction(event, added=True) or not self._own_night_reaction(event):
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
        if (await self.module_reaction(event, added=False) or not self._own_night_reaction(event)
                or self.notion is None):
            return
        link = await self.permalink(event["item"]["channel"], event["item"]["ts"])
        try:
            await asyncio.to_thread(self.notion.cancel_night_task, link)
        except NotionError as e:
            await self.notify_trouble(f"🌙 を外した Task を Notion で取り消せませんでした: {e}")

    async def run_agent(self, ws: Workspace, prompt: str, session_id: str | None = None,
                        channel: str = "", thread_ts: str = "",
                        on_activity=None, *, provider: str | None = None,
                        use_case: UseCase | None = None, request_text: str | None = None,
                        read_only: bool = False) -> runner.RunResult:
        """作業場で AI を1回動かす（研究テーマ・研究全体・自己改善）。担当のプロセス（A2A）があれば、そちらに頼む。

        担当は作業場で決まる（themes.actor_of。研究テーマを受け持つモジュールがあれば、そのモジュールの担当）。
        `use_case` を渡せば分類しない。`request_text` は分類に使う依頼者の文（引き継ぎメモや履歴の
        前置きを付ける前のもの）。渡さなければ prompt で分類する。

        どちらで動かしても、同じ制限の表（agent_policy.py）と `config.toml` の柵で動く。
        """
        actor = themes.actor_of(ws)
        agent = self.agents.get(actor)
        provider = provider or settings.selected_provider(self.config, self.store, actor)
        if provider not in PROVIDERS:
            # 分類器も動かせないので、ここで止めて App Home で選ぶよう伝える
            return runner.RunResult(is_error=True, errors=[NO_PROVIDER])
        if use_case is None:
            use_case, prompt = explicit_use_case(actor, prompt)
        if use_case is None:
            from kei_agent.model_classifier import UsageLimited, classify
            try:
                use_case = await classify(self.config, self.store, actor, request_text or prompt,
                                          provider=provider)
            except UsageLimited as e:
                return runner.RunResult(provider=provider, is_error=True, errors=[str(e)],
                                        limit_reset_at=e.reset_at)
        try:
            # 手動指定だけの用途は、依頼者の [[名前]] からしか来ない（分類器は選ばない）
            recipe = resolve(actor, provider, use_case, manual=is_manual(use_case))
        except ModelPolicyError as e:
            return runner.RunResult(is_error=True, errors=[str(e)])
        with self.claude_running():
            if agent is not None:
                # 担当のプロセスに、作業場（チャンネルの名前）と許可済みの接続先を添えて頼む
                return await agents.run_in_workspace(agent, ws, prompt, session_id, channel, thread_ts,
                                                     use_case, on_activity, provider=recipe.provider,
                                                     read_only=read_only)
            return await runner.run_model(
                self.config, runner.ExecutionRequest(ws, recipe, session_id, channel, thread_ts, read_only),
                prompt, on_activity,
            )

    async def ask_agent(self, actor: str, prompt: str, session_id: str | None = None,
                        channel: str = "", thread_ts: str = "", on_activity=None, *,
                        provider: str | None = None, read_only: bool = False,
                        use_case: UseCase | str | None = None) -> runner.RunResult:
        """モジュールのエージェントに自由な依頼（`ask`）を1回頼む。結果は研究の run_agent と同じ形。

        用途を渡さなければ、エージェントがその担当の分類器で決める。
        """
        agent = self.agents.get(actor)
        if agent is None:
            await self.notify_trouble(f"{actor} のエージェントの住所が config.toml の [a2a.agents] にありません")
            return runner.RunResult(is_error=True, errors=[f"{actor} のエージェントの住所がありません"])
        provider = provider or settings.selected_provider(self.config, self.store, actor)
        if provider not in PROVIDERS:
            return runner.RunResult(is_error=True, errors=[NO_PROVIDER])
        payload = {"prompt": prompt, "session_id": session_id, "channel": channel, "thread_ts": thread_ts,
                   "provider": provider, "read_only": read_only}
        if use_case is not None:
            payload["use_case"] = str(use_case)
        with self.claude_running():
            return await agents.run_ask(agent, payload, on_activity)

    # 決まった時刻の処理から使う（schedule.py）

    async def run_detached(self, ws: Workspace, channel_name: str, prompt: str, trigger: str,
                           *, actor: str = "",
                           use_case: UseCase | str | None = None) -> runner.RunResult:
        """スレッドを作らずに claude -p を動かす（定期処理用）。結果を見てから投稿先を決める。

        担当を渡さなければ、作業場の担当（研究テーマなら、テーマを受け持つモジュール）。
        """
        assert ws.cwd is not None
        actor = actor or themes.actor_of(ws)
        themes.ensure_workspace(ws)
        async with self.semaphore:
            run_id = self.store.start_run("", "", channel_name, trigger)
            if use_case is None:
                result = await self.run_agent(ws, prompt)
            else:
                try:
                    recipe = resolve_selected(self.config, self.store, actor, use_case)
                except ModelPolicyError as e:
                    result = runner.RunResult(is_error=True, errors=[str(e)])
                else:
                    agent = self.agents.get(actor) if actor in modules.known() else None
                    if agent is not None:
                        result = await agents.run_in_workspace(agent, ws, prompt, None, "", "", use_case,
                                                               provider=recipe.provider)
                    else:
                        result = await runner.run_model(
                            self.config, runner.ExecutionRequest(ws, recipe, None, "", ""), prompt,
                        )
            self.store.end_run(run_id, result.is_error, result.cost_usd)
            if result.limit_reset_at is not None:
                provider = result.provider
                if provider:
                    self.store.set_limit_until(provider, max(
                        self.store.limit_until(provider), self.limit_until(result.limit_reset_at)))
        await self.tell_failure(actor, result)
        return result

    async def publish(self, channel: str, channel_name: str, ws: Workspace, header: str,
                      result: runner.RunResult, footer: str = "", output_kind: str = "conversation") -> str:
        """見出しをチャンネルに投稿し、結果をそのスレッドに返す。スレッドで続きを話せるようにする。"""
        assert ws.cwd is not None
        resp = await self.slack.chat_postMessage(channel=channel, text=header)
        thread_ts = resp["ts"]
        req = Request(channel, channel_name, thread_ts, None, "")
        # セッションが作れなかった日でも、このスレッドへの返信には反応できるようにする
        self.store.upsert_thread(channel, thread_ts, channel_name, result.session_id)
        shown = ""
        if not result.is_error:
            try:
                shown = finalize_conversation(result.text)
                if output_kind == "daily":
                    shown = validate_daily(shown)
                elif output_kind == "review":
                    shown = validate_review(shown)
            except OutputError as e:
                # 形式を確かめる前の本文は出さない
                shown = ""
                log.warning("%s の出力契約に違反: %s", output_kind, e)
                result.is_error = True
                result.errors.append(f"invalid {output_kind} output")
        if shown:
            result.text = shown
            append_thread_log(ws.cwd, channel_name, thread_ts, "Kei Agent", shown)
            for chunk in split_text(shown):
                await self.post(req, chunk, markdown=True)
        if result.is_error:
            failure_kind = ("login" if result.failure_kind == "login"
                            else output_kind if output_kind in {"daily", "review"} else "connection")
            await self.post(req, safe_failure(failure_kind))
        if footer:
            await self.post(req, footer)
        return thread_ts

    def render_reply(self, result: runner.RunResult) -> tuple[str, bool]:
        """モデルの raw text を Slack 用の最終回答へ変換する唯一の入口。"""
        if result.is_error:
            if NO_PROVIDER in result.errors:
                return safe_failure("provider"), False
            if result.failure_kind == "login":
                return safe_failure("login"), False
            return safe_failure("timeout" if result.timed_out else "connection"), False
        try:
            return finalize_conversation(result.text), False
        except OutputError as e:
            # 本文はログに残さない。どの決まりに外れたかだけを残して、原因を追えるようにする
            log.warning("Slack 出力契約に違反: %s（%d 文字）", e, len(result.text or ""))
            return safe_failure("conversation"), True

    # 依頼の処理

    async def submit(self, req: Request) -> None:
        """依頼を受け付けて、裏で処理を始める。"""
        if req.trigger == "message":
            self.store.set_awaiting(req.channel, req.thread_ts, False)
        if req.trigger in ("message", "voice"):
            await self.drop_deferred_for(req)
        if req.message_ts:
            await self._react(self.slack.reactions_add, req.channel, req.message_ts, SEEN_REACTION)
        self.emit("working", theme=req.channel_name)
        self.spawn(self._process_and_report(req))

    async def _process_and_report(self, req: Request) -> None:
        """process() が落ちても、👀 がついたまま黙って終わらないようにする。"""
        try:
            await self.process(req)
        except Exception:
            log.exception("依頼の処理が落ちました")
            try:
                await self.post(req, safe_failure("connection"))
                # 👀 のまま残ると、答えたのかどうかが分からなくなる
                await self.mark_answered(req, failed=True)
            except Exception:
                log.exception("落ちたことをスレッドに伝えられません")
            await self.notify_trouble(f"#{req.channel_name} の依頼の処理が落ちました")

    async def process(self, req: Request) -> runner.RunResult | None:
        try:
            ws = themes.resolve(self.config, req.channel_name)
        except ValueError as e:
            log.warning("不正なテーマを指定されました: %s", e)
            await self.post(req, safe_failure("connection"))
            return None
        if await self.tell_if_waiting(req):
            return None
        if ws.kind is ChannelKind.IMPROVE:
            # Kei Agent のチャンネル。会話は、受け持つモジュール（core_channels。自己改善）が答える
            if not (ws.module and await self._dispatch(req, ws.module, "")):
                await self.post(req, NO_IMPROVE_OWNER)
                await self.mark_answered(req, failed=True)
            return None
        if ws.kind is ChannelKind.OVERVIEW and await self.route_overview(req):
            return None
        if ws.kind in (ChannelKind.THEME, ChannelKind.OVERVIEW) and themes.actor_of(ws) not in self.modules:
            # 研究テーマを受け持つモジュール（研究）がオフ
            await self.post(req, NO_THEME_OWNER)
            await self.mark_answered(req, failed=True)
            return None
        if ws.kind is ChannelKind.MODULE:
            await self._dispatch(req, themes.actor_of(ws), "")
            return None
        if ws.kind is ChannelKind.THEME and (owner := self.actor_for(req, ws)) in self.modules:
            # 研究テーマを受け持つモジュール（研究）が答える。作業場での会話の結果（core.work）は、夜間の Task の
            # ように結果を見て次を決める呼び出し元に返す
            self._work_results.pop(id(req), None)
            if await self._dispatch(req, owner, ""):
                return self._work_results.pop(id(req), None)
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
            except Exception:
                log.exception("依頼の処理に失敗しました")
                await self.post(req, safe_failure("connection"))
                # 👀 と「作業中」の表示を残したままにしない
                await self.mark_answered(req, failed=True)
                with suppress(Exception):
                    await self.thread_ui(req).finish("")
                return None
            finally:
                # 途中で落ちても、このテーマを「重なって動いている」ままにしない（何度呼んでもよい）
                self.theme_runs.end(req.channel_name, req.thread_ts)

    async def work_in_workspace(self, req: Request, actor: str, *, folder: Path | None = None,
                                hide: tuple[str, ...] = ()) -> runner.RunResult | None:
        """チャンネルの作業場で、その担当と会話して答える（api.Core.work）。

        研究テーマのチャンネルは、テーマのフォルダ。folder を渡したとき（モジュールのフォルダの中）は、モジュールが
        会話を受け持つチャンネル（モジュールのチャンネル、Kei Agent のチャンネル）で、そのフォルダを作業場にする。
        hide に書いた頭で始まる行は、Slack に出さない（合図の行など）。
        添付の保存・できたファイルの添付・接続先の許可・引き継ぎの提案・ジョブは、研究と同じ流れ。
        モジュールの on_message から呼ばれる（スレッドのロックと同時実行の上限は、取り次いだ _dispatch が持っている）。
        """
        ws = themes.resolve(self.config, req.channel_name)
        if folder is not None:
            if ws.kind not in (ChannelKind.MODULE, ChannelKind.IMPROVE) or themes.actor_of(ws) != actor:
                raise ValueError(f"#{req.channel_name} は、{actor} が会話を受け持つチャンネルではありません")
            folder.mkdir(parents=True, exist_ok=True)
            ws = replace(ws, cwd=folder)
        else:
            if ws.kind is not ChannelKind.THEME or themes.actor_of(ws) != actor:
                raise ValueError(f"#{req.channel_name} は、{actor} が受け持つ研究テーマのチャンネルではありません")
            themes.ensure_workspace(ws)
            ws = replace(ws, allowed_domains=tuple(settings.theme_domains(self.store, ws.channel_name)))
            if ws.channel_name not in self.registered_themes:
                # 招待のイベントを取りこぼしていても、1テーマ = 1チャンネル = 1ディレクトリ = Notion の1行を保つ
                self.registered_themes.add(ws.channel_name)
                await self.register_theme(req.channel, ws)
        try:
            result = await self.run(req, ws, hide=hide)
            self._work_results[id(req)] = result
            return result
        except Exception:
            log.exception("依頼の処理に失敗しました")
            await self.post(req, safe_failure("connection"))
            await self.mark_answered(req, failed=True)
            with suppress(Exception):
                await self.thread_ui(req).finish("")
            return None
        finally:
            self.theme_runs.end(req.channel_name, req.thread_ts)

    async def route_overview(self, req: Request) -> bool:
        """研究全体のチャンネルで、ほかのエージェントの用事なら、そちらに回す（回したら True）。

        朝のまとめがここに出るので、「この課題は？」「今日の会議は？」にも答えられるようにする。
        スレッドの続きは、最初に答えた相手のまま続ける（毎回は判定しない）。
        """
        if req.trigger not in ("message", "voice") or not self.agents:
            return False
        answered = self.store.thread_agent(req.channel, req.thread_ts)
        if (answered in self.agents or answered in self.modules) and await self._dispatch(req, answered, ""):
            return True
        row = self.store.get_thread(req.channel, req.thread_ts)
        if row is not None and row["session_id"]:
            return False    # 研究の会話の続き
        catalog = {name: skills for name in self.agents if (skills := await self.skills_of(name))}
        if not catalog:
            return False
        await self.thread_ui(req).activity(router.STATUS_TEXT)
        choice = await router.pick_across(self.config, catalog, req.text, store=self.store)
        return await self._dispatch(req, choice.agent, choice.skill, choice.params)

    def actor_for(self, req: Request, ws: Workspace) -> str:
        """その依頼に答える担当。テーマのチャンネルでも、モジュールが引き取ったスレッド（朝の論文の新着など）は、
        そのモジュールが答える（api.Core.claim_thread）。"""
        if ws.kind is ChannelKind.THEME:
            owner = self.store.thread_agent(req.channel, req.thread_ts)
            if owner in self.modules:
                return owner
        return themes.actor_of(ws)

    async def _dispatch(self, req: Request, agent: str, skill: str, params: dict | None = None) -> bool:
        """エージェントかモジュールに渡す。同じスレッドで2つ同時に動かさず、全体の同時実行の上限も守る。

        モジュールには class Module の on_message で渡す。skill と params は、研究全体のチャンネルで
        振り分け係が選んだ仕事（無ければ空。モジュールは core.pick_skill で自分で選べる）。
        """
        on_message = getattr(self.modules.get(agent), "on_message", None)
        if on_message is None:
            return False
        # このスレッドを覚えておく。覚えていないと、メンションなしの返信（on_message）を拾えず、
        # 研究全体から回した続きも、毎回どこに聞くかを選び直してしまう
        self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, None)
        self.store.set_agent_session(req.channel, req.thread_ts, agent,
                                     self.store.agent_session(req.channel, req.thread_ts, agent) or "")
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
            await on_message(req, skill=skill, params=dict(params or {}))
        return True

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

    async def run(self, req: Request, ws: Workspace, hide: tuple[str, ...] = ()) -> runner.RunResult:
        """1回分の依頼を claude に渡し、結果をスレッドに返す。スレッドのロックを取ってから呼ぶ。

        hide に書いた頭で始まる行は、Slack に出さない（合図の行など。返す結果の本文には残す）。
        """
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
        except asyncio.CancelledError:
            # 終了処理などで止められた回。控えは残したまま（次の起動でやり直す）、
            # 走りっぱなしの記録だけ閉じる
            self.store.end_run(run_id, is_error=True, cost_usd=None)
            self.theme_runs.end(req.channel_name, req.thread_ts)
            raise
        except Exception:
            self.store.finish_deferred(in_flight)
            self.store.end_run(run_id, is_error=True, cost_usd=None)
            self.theme_runs.end(req.channel_name, req.thread_ts)
            raise
        self.store.finish_deferred(in_flight)
        self.store.end_run(run_id, result.is_error, result.cost_usd)
        if req.trigger in ("message", "voice"):
            self.store.count_turn(req.channel, req.thread_ts)
        self.store.set_stalled(req.channel, req.thread_ts, req.text if result.is_error else None)

        if result.limit_reset_at is not None:
            await self.defer_for_limit(req, result.limit_reset_at,
                                       result.provider or "")
            self.store.set_awaiting(req.channel, req.thread_ts, True)
            await ui.finish("")
            await self.mark_answered(req, failed=True)
            self.theme_runs.end(req.channel_name, req.thread_ts)
            return result

        connect = self.new_connect_requests(ws, result.text, result.requested_domains)
        awaiting = req.awaiting_after or result.is_error or AWAITING_MARKER in result.text or bool(connect)
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        if awaiting:
            self.emit("awaiting", theme=req.channel_name)
        await self.sync_review_conclusion(req)
        await self._reply(req, ws, ui, result, awaiting, hide)
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

    async def _converse(self, req: Request, ws: Workspace | None, prompt: str, ui: ThreadUI | None,
                        actor: str | None = None) -> runner.RunResult:
        """このスレッドの会話の続きとして担当の AI を動かす。会話が失われていたら、Slack の履歴から戻す。

        研究・自己改善は run_agent、モジュール（大学・仕事・知識など）はそのエージェントの `ask` に頼む。会話の続け方
        （session の版、履歴からの戻し、session が消えていたときのやり直し）はどの担当も同じ。
        ui がなければ、経過を Slack に見せずに動かす（引き継ぎメモを書かせるときなど）。
        """
        # ジョブの依頼をこのスレッドのものとして確かめられるよう、先にスレッドを記録する
        row = self.store.get_thread(req.channel, req.thread_ts)
        self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, None)
        actor = actor or (themes.actor_of(ws) if ws is not None else "")
        provider = settings.selected_provider(self.config, self.store, actor)
        version = prompt_version(self.config, actor)
        session_id = self.store.session_for(req.channel, req.thread_ts, actor, provider, version)
        prior_provider = self.store.last_provider(req.channel, req.thread_ts, actor)
        # [[research-design]] などの用途の指定は依頼者の文の先頭にある（どの担当でも同じ書き方）。前置きを付ける前に読み取る
        use_case, prompt = explicit_use_case(actor, prompt)
        request_text = prompt
        # 区切って立てたスレッドの最初の回には、前のスレッドの引き継ぎメモを渡す
        prompt = self.handoff_memo_for(row) + prompt
        stalled = row["stalled_request"] if row else None
        if stalled or (row is not None and row["session_id"] and not session_id) or (
            prior_provider is not None and prior_provider != provider
        ):
            # 止まった回は provider 側に記録が残らないことがあるので、resume せず Slack の履歴から文脈を戻す
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            prompt = history_prompt(messages, self.bot_user_id, prompt, req.message_ts,
                                    stalled if stalled and stalled != req.text else None, dropped=dropped)
            session_id = None
        elif session_id is None and req.message_ts and req.message_ts != req.thread_ts:
            # Kei Agent の投稿（朝の読みもの、論文の新着、朝の一覧など）への最初の返信。
            # 「2番を詳しく」に答えられるよう、元の投稿を渡す（人が始めたスレッドは今までどおり）
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            parent = next((m for m in messages if m.get("ts") == req.thread_ts), None)
            if parent is not None and (parent.get("user") == self.bot_user_id or parent.get("bot_id")):
                prompt = history_prompt(messages, self.bot_user_id, prompt, req.message_ts, dropped=dropped,
                                        reply_to_post=True)

        on_activity = ui.activity if ui is not None else None

        async def attempt(prompt: str, session_id: str | None) -> runner.RunResult:
            # 今日の日付と曜日は、どの担当にも同じ形で先頭に付ける（「今日の授業は？」に答えられるように）
            prompt = today_line() + prompt
            if ws is None:
                # 作業場を本体に持たない担当（モジュール）
                return await self.ask_agent(actor, prompt, session_id, req.channel, req.thread_ts,
                                            on_activity, provider=provider, use_case=use_case)
            return await self.run_agent(ws, prompt, session_id, req.channel, req.thread_ts,
                                        on_activity, provider=provider,
                                        use_case=use_case, request_text=request_text)

        result = await attempt(prompt, session_id)
        if result.session_missing:
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            result = await attempt(
                history_prompt(messages, self.bot_user_id, prompt, req.message_ts, dropped=dropped), None)
        await self.tell_failure(actor, result)
        if not result.is_error:
            if result.session_id:
                self.store.set_session(req.channel, req.thread_ts, actor, provider,
                                       result.session_id, version)
                # 既存 row の session_id は provider 不明の legacy 値なので上書きしない。
                # 新規スレッドは従来互換の参照値として保存する。
                if row is None or row["session_id"] is None:
                    self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, result.session_id)
                self.store.set_prompt_version(req.channel, req.thread_ts, version)
            else:
                self.store.set_last_provider(req.channel, req.thread_ts, actor, provider)
        return result

    async def answer_question(self, actor: str, question: str, theme: str = "") -> str:
        """声のレイヤからの問い合わせ（本体の A2A の口、questions.py）。

        担当を呼べるのは本体だけ。どの担当にも読むだけで頼み、Slack に出すときと同じ出力の確認を通す。
        """
        spec = modules.known().get(actor)
        module_actor = actor in self.config.modules and spec is not None and spec.actor is not None
        if actor not in VOICE_USE_CASES and not module_actor:
            return "研究、授業、仕事のどれを調べるか分からなかった。"
        if not question.strip():
            return "何を調べるか分からなかった。"
        prompt = today_line() + question.strip()
        # モジュールの担当は、module.toml の default_use_case（読むだけの1回）で答える
        use_case = VOICE_USE_CASES.get(actor) or spec.actor.default_use_case
        if modules.use_case_owner(str(use_case)) is None:
            use_case = spec.actor.default_use_case
        if actor == themes.catch_all_module(self.config):
            if not theme.strip():
                return "どの研究テーマを調べるかも教えて。"
            try:
                ws = themes.resolve(self.config, theme.strip().lstrip("#"))
            except ValueError:
                return "その研究テーマは使えない名前だった。"
            if ws.kind is not ChannelKind.THEME:
                return "その名前は研究テーマではなかった。"
            result = await self.run_agent(ws, prompt, use_case=use_case, read_only=True)
        else:
            result = await self.ask_agent(actor, prompt, read_only=True, use_case=use_case)
        answer, _ = self.render_reply(result)
        return answer

    async def tell_failure(self, actor: str, result: runner.RunResult) -> None:
        """担当の AI が答えられなかった理由をログに残す。ログインが切れていたら、改善のチャンネルに1回だけ知らせる。

        ログインが切れると、入り直すまで何度頼んでも動かない。スレッドには固定の文しか出さないので、
        入り直し方はこちらで知らせる（知らせた目印は毎晩の保守で60日たつと消え、切れたままなら、また知らせる）。
        """
        if not result.is_error or NO_PROVIDER in result.errors:
            return
        log.warning("%s の担当が答えられませんでした: %s", actor, result.failure_reason())
        key = f"login:{actor}:{result.provider or ''}"
        if result.failure_kind != "login" or self.store.noticed(key):
            return
        self.store.record_notice(key)
        how = result.errors[0] if result.errors else "ログインが切れている"
        await self.notify_trouble(f"{agent_labels(self.config).get(actor, actor)}の担当の AI が動きません。{how}")

    def default_question(self, actor: str) -> str:
        """メンションだけで本文が無いときに、担当に聞くこと。"""
        return getattr(self.modules.get(actor), "default_question", "") or "何ができるか教えて"

    async def converse_with_agent(self, req: Request, actor: str) -> runner.RunResult:
        """モジュールの自由な質問。研究と同じ流れ（会話の続き・経過・上限・出力の確認・再起動からのやり直し）。

        研究と違って本体に作業場を持たないので、添付の保存・スレッドのログ・出力の添付はない。
        スレッドのロックと同時実行の上限は、呼び出し側（_dispatch）が持つ。
        """
        ui = self.thread_ui(req)
        await ui.start()
        run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)
        # 途中で終了させられても、次の起動で拾ってやり直せるように控えておく
        in_flight = self.store.start_in_flight(req.to_payload())
        try:
            result = await self._converse(req, None, req.text or self.default_question(actor), ui, actor=actor)
        except asyncio.CancelledError:
            self.store.end_run(run_id, is_error=True, cost_usd=None)
            raise
        except Exception:
            self.store.finish_deferred(in_flight)
            self.store.end_run(run_id, is_error=True, cost_usd=None)
            raise
        self.store.finish_deferred(in_flight)
        self.store.end_run(run_id, result.is_error, result.cost_usd)
        if req.trigger in ("message", "voice"):
            self.store.count_turn(req.channel, req.thread_ts)
        self.store.set_stalled(req.channel, req.thread_ts, req.text if result.is_error else None)
        if result.limit_reset_at is not None:
            await self.defer_for_limit(req, result.limit_reset_at, result.provider or "")
            self.store.set_awaiting(req.channel, req.thread_ts, True)
            await ui.finish("")
            await self.mark_answered(req, failed=True)
            return result
        answer, _ = self.render_reply(result)
        awaiting = result.is_error or AWAITING_MARKER in result.text
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        streamed = await ui.finish(answer, awaiting and not result.is_error)
        if not streamed:
            for chunk in split_text(answer):
                await self.post(req, chunk, markdown=True)
        await self.mark_answered(req, result.is_error)
        return result

    async def _reply(self, req: Request, ws: Workspace, ui: ThreadUI, result: runner.RunResult,
                     awaiting: bool, hide: tuple[str, ...] = ()) -> None:
        """まとめをスレッドに返す。流して見せられなかったときだけ、まとめて投稿する。"""
        assert ws.cwd is not None
        shown, contract_failed = self.render_reply(result)
        # 合図の行（モジュールが hide で渡したもの。着手・取り込みなど）は、検出に使うだけで Slack には出さない
        # （result.text は残す）。区切りの合図は、題をボタンに出すので本文からは消す
        shown = strip_handoff(strip_lines(shown, hide))
        streamed = await ui.finish(shown, awaiting and not result.is_error)
        if shown:
            append_thread_log(ws.cwd, req.channel_name, req.thread_ts, "Kei Agent", shown)
            if not streamed:
                for chunk in split_text(shown):
                    await self.post(req, chunk, markdown=True)
        if contract_failed:
            log.warning("Slack 出力契約に違反した応答を破棄しました: channel=%s thread=%s", req.channel, req.thread_ts)

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
        except Exception:
            # 結果はもう返しているので、添付だけ失敗したことを伝える
            log.exception("outputs/ のファイルを添付できません")
            notes.append("結果のファイルを添付できなかったよ。もう一度頼んでね。")
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
        if req.trigger != "message":
            return
        link = self.store.notion_link(req.channel, req.thread_ts)
        if link is None or link["kind"] != "review" or not req.text.strip():
            return
        if self.hub is None:
            await self.notify_trouble("振り返りの結論を日別記録に保存できません。共通 Notion ホームの共有を確認してください")
            return
        try:
            await asyncio.to_thread(self.hub.append_review_conclusion, link["page_id"], req.text,
                                    datetime.now(), req.message_ts)
        except NotionError as e:
            await self.notify_trouble(f"振り返りの結論を Notion に追記できませんでした: {e}")

    # Slack の外からの依頼（声のレイヤなど。docs/architecture.md）

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

    async def defer_for_limit(self, req: Request, reset_at: float, provider: str) -> None:
        """上限に達した依頼を、明けてからやり直すものとして覚えておく。"""
        until = self.limit_until(reset_at)
        if not provider:
            raise ValueError("provider が未選択です")
        self.store.set_limit_until(provider, max(self.store.limit_until(provider), until))
        self.store.defer_run("request", {**req.to_payload(), "provider": provider}, until)
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        await self.post(req, f"{FAILED_PREFIX} {provider} の利用上限に達したみたい。{when} ごろに自動でやり直すね。")
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
            await self.submit(replace(req, text=interrupted_prompt(req.text)))
        if interrupted:
            log.info("再起動で止まっていた依頼を %d 件やり直します", len(interrupted))
        return len(interrupted)

    async def retry_deferred(self, now: float | None = None) -> None:
        """上限で止まった依頼を、明けたらやり直す。"""
        now = time.time() if now is None else now
        for deferred_id, payload in self.store.due_deferred("request", now):
            self.store.finish_deferred(deferred_id)
            req = Request.from_payload(payload)
            original_provider = payload.get("provider")
            if original_provider:
                actor = self.actor_for(req, themes.resolve(self.config, req.channel_name))
                if settings.selected_provider(self.config, self.store, actor) != original_provider:
                    await self.post(req, "使うモデルが切り替わったので、この依頼は自動で再実行しなかったよ。必要ならもう一度頼んでね。")
                    continue
            await self.submit(req)

    # ジョブ

    async def handle_job_requests(self, cwd: Path) -> None:
        for o in await self.jobs.process_requests(cwd):
            # 依頼のチャンネルとスレッドは Claude が書いたものなので、知っているスレッドのときだけ投稿する
            known = bool(o.channel and o.thread_ts and self.store.get_thread(o.channel, o.thread_ts))
            req = Request(o.channel, "", o.thread_ts, None, "")
            if o.error:
                text = f"ジョブ「{o.job.name}」を投入できなかったよ: {o.error}" if o.job else o.error
                if known:
                    await self.post(req, f"{FAILED_PREFIX} {text}")
                else:
                    await self.notify_trouble(f"`{cwd}` のジョブの依頼: {text}")
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
