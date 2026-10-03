"""ローカルの依頼を受けて、claude -p とジョブを動かし、スレッドに返す。

通知の保存は `slack` に受け取る Outbox の互換 API を使う。
処理は担当への取り次ぎ・利用上限・バックグラウンドの依頼に分ける。
通知は Outbox に保存し、Dot が利用者に届ける。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Coroutine
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from kei_agent import api
from kei_agent.configuration.config import Config
from kei_agent.conversation import router
from kei_agent.conversation.auto_messages import (
    handoff_memo_for,
    history_prompt,
    today_line,
)
from kei_agent.conversation.background import BackgroundLoops
from kei_agent.conversation.limits import LimitDeferral
from kei_agent.conversation.module_bridge import ModuleBridge
from kei_agent.conversation.request import Request
from kei_agent.conversation.response_output import (
    OutputError,
    finalize_conversation,
    safe_failure,
    trouble_message,
)
from kei_agent.conversation.slack_text import (
    AWAITING_MARKER,
    FAILED_PREFIX,
    is_status_inquiry,
    split_text,
    strip_lines,
)
from kei_agent.conversation.startup_checks import StartupChecks
from kei_agent.execution import a2a, agents, guard, runner, updates
from kei_agent.execution.execution_contract import prompt_version
from kei_agent.execution.jobs import JobManager
from kei_agent.execution.model_policy import (
    PROVIDERS,
    ModelPolicyError,
    UseCase,
    explicit_use_case,
    is_manual,
    resolve,
)
from kei_agent.framework import modules
from kei_agent.storage import settings
from kei_agent.storage.notion import NotionError
from kei_agent.storage.notion_hub import HubStore
from kei_agent.storage.notion_store import NotionStore
from kei_agent.storage.records import Records
from kei_agent.storage.store import Store
from kei_agent.workspaces import themes
from kei_agent.workspaces.theme_files import (
    append_thread_log,
    changed_files,
    snapshot_outputs,
    split_uploads,
)
from kei_agent.workspaces.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

# これ以上かかった作業が終わったら、依頼者に通知の別投稿を送る（短い依頼には送らない）
NOTIFY_AFTER_SECONDS = 60
# 依頼者が画面を見ていないはずの回（ジョブの完了で再開した回）。短くても、終わったら知らせる
UNATTENDED_TRIGGERS = ("job",)
# 問題の知らせは、前の知らせからこの秒数のうちに起きたものを、同じ1通に書き足す（通知が鳴るのは最初の1回）
TROUBLE_GROUP_SECONDS = 30 * 60
# 保存した会話からプロンプトに載せる上限（新しいものを残す）
HISTORY_MAX_MESSAGES = 600
# スレッドのログに残すときの、依頼の出どころの呼び名
WHO_BY_TRIGGER = {"job": "Kei Agent（ジョブ完了）", "voice": "依頼者（声）"}


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
                    "agents.csv の improve を enabled=true にしてね。")
# 研究テーマ（ほかのどれにも当たらないチャンネル）を受け持つモジュールが、オンになっていないとき
NO_THEME_OWNER = ("このチャンネルを受け持つモジュールがないよ。研究テーマに使うなら、agents.csv の research を"
                  " enabled=true にしてね。")
# 声からの問い合わせで使う、読むだけの軽い用途（研究のモジュールの用途。無ければ担当の default_use_case）
VOICE_USE_CASES = {"research": "research_extract"}


class Assistant(StartupChecks, ModuleBridge, LimitDeferral,
                BackgroundLoops):
    # 明ける時刻が分からないときや、返ってきた時刻が過去だったときに待つ時間
    LIMIT_FALLBACK_SECONDS = 30 * 60
    # 明けた直後に詰まらないよう、少しだけ余分に待つ
    LIMIT_MARGIN_SECONDS = 60

    def __init__(self, config: Config, store: Store, slack, jobs: JobManager,
                 notion: NotionStore | None = None,
                 hub: HubStore | None = None):
        self.config = config
        self.notion = notion
        self.hub = hub
        self.store = store
        self.slack = slack
        self.jobs = jobs
        self.conversation_records = Records(store, "conversation")
        self.semaphore = asyncio.Semaphore(config.max_concurrent_runs)
        # 書き足している問題の知らせ（channel・ts・texts・at）
        self._trouble: dict | None = None
        self._trouble_lock = asyncio.Lock()
        # スレッドごとのロックは捨てずに残す。「待っている依頼がいるか」は release の直後に
        # 一瞬だけ「いない」と見えるので、そこで捨てると、待っていた依頼が別のロックを取り、
        # 同じスレッド（同じセッション）の claude が2本同時に走る
        self.thread_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        # MCP（hands.py）で動いている作業（受付番号 → 作業）。本体が起動し直すと空に戻る
        self.hands_tasks: dict[str, asyncio.Task] = {}
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
        # この起動が、入れ替えたあとのものなら、その結果（本体が起動したあとに take_update で読む）
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
        """本体が起動したあとに呼ぶ。入れ替えたあとの起動なら、その結果を覚えて、印を消す（deploy/run.sh が戻さない）。"""
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
        """起動して 本体が起動したあと（class Module の on_start）。1つが落ちても、ほかは続ける。"""
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

    def is_allowed(self, user: str | None) -> bool:
        return guard.is_owner(self.config, user)

    # 通知と会話の記録

    async def channel_name(self, channel: str) -> str:
        """テーマの名前（チャンネル名から、並び順のための番号を外したもの）。"""
        if channel not in self.channel_names:
            info = await self.slack.conversations_info(channel=channel)
            self.channel_names[channel] = themes.theme_name(info["channel"]["name"])
        return self.channel_names[channel]

    async def channel_ids(self) -> dict[str, str]:
        """Outbox が通知先に使うチャンネルの、名前から論理 ID への対応。"""
        ids: dict[str, str] = {}
        cursor = None
        while True:
            resp = await self.slack.conversations_list(
                types="public_channel,private_channel", exclude_archived=True, limit=200, cursor=cursor
            )
            for c in resp.get("channels", []):
                if c.get("is_member"):
                    # 番号つきの名前（`1-amr-query`）でも、テーマ名（`amr-query`）でも引けるようにする
                    ids[c["name"]] = ids[themes.theme_name(c["name"])] = c["id"]
                    self.channel_names[c["id"]] = themes.theme_name(c["name"])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return ids

    def remember_message(self, channel: str, thread_ts: str, user: str, text: str, ts: str | None = None) -> str:
        """会話を端末の記録に残す。再起動や provider 変更のあとも読み直せる。"""
        if user not in ("owner", "assistant"):
            raise ValueError("会話の発言者は owner か assistant を指定してください")
        ts = ts or f"{time.time_ns()}"
        key = f"{channel}/{thread_ts}/{ts}"
        previous = self.conversation_records.get("message", key)
        self.conversation_records.put("message", key, {
            "ts": ts, "user": user, "text": text, "channel": channel, "thread_ts": thread_ts,
            "at": previous["at"] if previous else time.time(),
        })
        return ts

    async def thread_messages(self, channel: str, thread_ts: str) -> tuple[list[dict], int]:
        messages = [message for message in self.conversation_records.items("message")
                    if message["channel"] == channel and message["thread_ts"] == thread_ts]
        by_ts = {message["ts"]: message for message in messages}
        for notice in self._thread_notices(channel, thread_ts):
            by_ts.setdefault(notice["ts"], notice)
        messages = sorted(by_ts.values(), key=lambda message: message["at"])
        dropped = max(0, len(messages) - HISTORY_MAX_MESSAGES)
        return messages[-HISTORY_MAX_MESSAGES:], dropped

    def _thread_notices(self, channel: str, thread_ts: str) -> list[dict]:
        """モジュールが直接 Outbox に出した親の通知や返信も、会話の材料にする。"""
        records = getattr(self.slack, "records", None)
        if records is None:
            return []
        return [{"ts": notice["id"], "user": "assistant", "text": notice["text"], "at": notice["at"]}
                for notice in records.items("notice")
                if notice["channel"] == channel and (notice["thread_ts"] or notice["id"]) == thread_ts]

    async def fetch_message(self, channel: str, ts: str) -> dict | None:
        saved = next((message for message in self.conversation_records.items("message")
                      if message["channel"] == channel and message["ts"] == ts), None)
        return saved or next((message for message in self._thread_notices(channel, ts) if message["ts"] == ts), None)

    async def mark_answered(self, req: Request, failed: bool) -> None:
        self.emit("failed" if failed else "done", theme=req.channel_name)

    async def permalink(self, channel: str, ts: str) -> str:
        # Slack の投稿リンクは Dot が持つ。本体の会話番号からは生成できない。
        return ""

    async def post(self, req: Request, text: str, markdown: bool = False) -> None:
        if markdown:
            posted = await self.slack.chat_postMessage(channel=req.channel, thread_ts=req.thread_ts, markdown_text=text)
        else:
            posted = await self.slack.chat_postMessage(channel=req.channel, thread_ts=req.thread_ts, text=text)
        self.remember_message(req.channel, req.thread_ts, "assistant", text, posted["ts"])

    async def notify_owner(self, req: Request, text: str) -> None:
        """スレッド内の投稿は通知が来ないので、依頼者へのメンション付きの短い投稿を足す。"""
        try:
            await self.post(req, f"<@{self.config.allowed_user_id}> {text}")
        except Exception:
            log.warning("依頼者への通知を投稿できません", exc_info=True)

    async def notify_trouble(self, text: str) -> None:
        """詳細はログに残し、改善チャンネルには何が起きたかを1行で知らせる（パスは名前だけ、長さは切る）。

        Notion が止まると、同じ原因で続けていくつも失敗する。前の知らせから TROUBLE_GROUP_SECONDS のうちなら、
        新しく投稿せずに前の1通へ書き足す（Slack は書き換えでは通知を鳴らさない）。
        """
        log.warning(text)
        try:
            ids = await self.channel_ids()
            channel = next((ids[n] for n in self.config.improve_channels if n in ids), None)
            if not channel:
                return
            async with self._trouble_lock:
                now = time.time()
                last = self._trouble
                if last and last["channel"] == channel and now - last["at"] < TROUBLE_GROUP_SECONDS:
                    last["texts"].append(text)
                    last["at"] = now
                    await self.slack.chat_update(channel=channel, ts=last["ts"], text=trouble_message(last["texts"]))
                    return
                posted = await self.slack.chat_postMessage(channel=channel, text=trouble_message([text]))
                self._trouble = {"channel": channel, "ts": posted["ts"], "texts": [text], "at": now}
        except Exception:
            log.exception("Kei Agent の改善のチャンネルに知らせられません")

    async def register_theme(self, channel: str, ws: Workspace) -> None:
        if self.notion is None:
            return
        try:
            await asyncio.to_thread(self.notion.ensure_theme, ws.channel_name, "", f"{ws.cwd}/")
        except NotionError as e:
            await self.notify_trouble(f"Notion にテーマ「{ws.channel_name}」を登録できませんでした: {e}")

    async def run_agent(self, ws: Workspace, prompt: str, session_id: str | None = None,
                        channel: str = "", thread_ts: str = "",
                        on_activity=None, *, provider: str | None = None,
                        use_case: UseCase | None = None, request_text: str | None = None,
                        read_only: bool = False, for_head: bool = False,
                        timeout_minutes: int | None = None) -> runner.RunResult:
        """作業場で AI を1回動かす（研究テーマ・研究全体・自己改善）。担当のプロセス（A2A）があれば、そちらに頼む。

        担当は作業場で決まる（themes.actor_of。研究テーマを受け持つモジュールがあれば、そのモジュールの担当）。
        `use_case` を渡せば分類しない。`request_text` は分類に使う依頼者の文（引き継ぎメモや履歴の
        前置きを付ける前のもの）。渡さなければ prompt で分類する。

        どちらで動かしても、同じ制限の表（agent_policy.py）と `config.toml` の柵で動く。
        """
        actor = themes.actor_of(ws)
        agent = self.agents.get(actor)
        provider = provider or settings.selected_provider(self.config, actor)
        if provider not in PROVIDERS:
            # 分類器も動かせないので、ここで止めて agents.csv で選ぶよう伝える
            return runner.RunResult(is_error=True, errors=[NO_PROVIDER])
        if use_case is None:
            use_case, prompt = explicit_use_case(actor, prompt)
        if use_case is None:
            from kei_agent.execution.model_classifier import UsageLimited, classify
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
                # 担当のプロセスに、作業場（チャンネルの名前）を添えて頼む
                return await agents.run_in_workspace(agent, ws, prompt, session_id, channel, thread_ts,
                                                     use_case, on_activity, provider=recipe.provider,
                                                     read_only=read_only, for_head=for_head,
                                                     timeout_minutes=timeout_minutes)
            return await runner.run_model(
                self.config, runner.ExecutionRequest(ws, recipe, session_id, channel, thread_ts, read_only,
                                                     for_head=for_head, timeout_minutes=timeout_minutes),
                prompt, on_activity,
            )

    async def ask_agent(self, actor: str, prompt: str, session_id: str | None = None,
                        channel: str = "", thread_ts: str = "", on_activity=None, *,
                        provider: str | None = None, read_only: bool = False,
                        use_case: UseCase | str | None = None, for_head: bool = False,
                        timeout_minutes: int | None = None) -> runner.RunResult:
        """モジュールのエージェントに自由な依頼（`ask`）を1回頼む。結果は研究の run_agent と同じ形。

        用途を渡さなければ、エージェントがその担当の分類器で決める。
        """
        agent = self.agents.get(actor)
        if agent is None:
            await self.notify_trouble(f"{actor} のエージェントの住所が config.toml の [a2a.agents] にありません")
            return runner.RunResult(is_error=True, errors=[f"{actor} のエージェントの住所がありません"])
        provider = provider or settings.selected_provider(self.config, actor)
        if provider not in PROVIDERS:
            return runner.RunResult(is_error=True, errors=[NO_PROVIDER])
        payload = {"prompt": prompt, "session_id": session_id, "channel": channel, "thread_ts": thread_ts,
                   "provider": provider, "read_only": read_only, "for_head": for_head,
                   "timeout_minutes": timeout_minutes}
        if use_case is not None:
            payload["use_case"] = str(use_case)
        with self.claude_running():
            return await agents.run_ask(agent, payload, on_activity)

    # 決まった時刻の処理から使う（schedule.py）

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
            if await self.hold_until_limit_ends(req):
                return
            await self.drop_deferred_for(req)
        self.emit("working", theme=req.channel_name)
        self.spawn(self._process_and_report(req))

    async def _process_and_report(self, req: Request) -> None:
        """process() が落ちても、失敗の通知とイベントを残す。"""
        try:
            await self.process(req)
        except Exception:
            log.exception("依頼の処理が落ちました")
            try:
                await self.post(req, safe_failure("connection"))
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
        if ws.kind is ChannelKind.OVERVIEW and (owner := self.actor_for(req, ws)) != themes.actor_of(ws) \
                and await self._dispatch(req, owner, ""):
            # 研究全体のチャンネルで、モジュールが引き取ったスレッド（振り返りの会話など）
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
        if ws.kind is ChannelKind.PROJECT:
            # プロジェクトのチャンネル。受け持つモジュール（仕事など）が、作業場で答える
            if not await self._dispatch(req, themes.actor_of(ws), ""):
                await self.post(req, NO_THEME_OWNER)
                await self.mark_answered(req, failed=True)
            return self._work_results.pop(id(req), None)
        if ws.kind is ChannelKind.THEME and (owner := self.actor_for(req, ws)) in self.modules:
            # 研究テーマを受け持つモジュール（研究）が答える。作業場での会話の結果（core.work）は、夜間の Task の
            # ように結果を見て次を決める呼び出し元に返す
            self._work_results.pop(id(req), None)
            if await self._dispatch(req, owner, ""):
                return self._work_results.pop(id(req), None)
        themes.ensure_workspace(ws)
        if ws.kind is ChannelKind.THEME and ws.channel_name not in self.registered_themes:
            # 招待のイベントを取りこぼしていても、1テーマ = 1チャンネル = 1ディレクトリ = Notion の1行を保つ
            self.registered_themes.add(ws.channel_name)
            await self.register_theme(req.channel, ws)
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
            try:
                return await self.run(req, ws)
            except Exception:
                log.exception("依頼の処理に失敗しました")
                await self.post(req, safe_failure("connection"))
                await self.mark_answered(req, failed=True)
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
        保存済みの添付・できたファイルの通知・ジョブは、研究と同じ流れ。
        モジュールの on_message から呼ばれる（スレッドのロックと同時実行の上限は、取り次いだ _dispatch が持っている）。
        """
        ws = themes.resolve(self.config, req.channel_name)
        if folder is not None:
            if ws.kind not in (ChannelKind.MODULE, ChannelKind.IMPROVE) or themes.actor_of(ws) != actor:
                raise ValueError(f"#{req.channel_name} は、{actor} が会話を受け持つチャンネルではありません")
            folder.mkdir(parents=True, exist_ok=True)
            ws = replace(ws, cwd=folder)
        else:
            if ws.kind not in (ChannelKind.THEME, ChannelKind.PROJECT) or themes.actor_of(ws) != actor:
                raise ValueError(f"#{req.channel_name} は、{actor} が受け持つ研究テーマ・プロジェクトのチャンネルではありません")
            themes.ensure_workspace(ws)
            if ws.kind is ChannelKind.THEME and ws.channel_name not in self.registered_themes:
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
        self.emit("working", theme=req.channel_name)
        choice = await router.pick_across(self.config, catalog, req.text, store=self.store)
        return await self._dispatch(req, choice.agent, choice.skill, choice.params)

    def actor_for(self, req: Request, ws: Workspace) -> str:
        """その依頼に答える担当。テーマと研究全体のチャンネルでも、モジュールが引き取ったスレッド（朝の論文の新着、
        振り返りの会話など）は、そのモジュールが答える（api.Core.claim_thread）。"""
        if ws.kind in (ChannelKind.THEME, ChannelKind.OVERVIEW):
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
        # この会話を覚えておき、次の依頼も同じ担当へ渡せるようにする。
        # 研究全体から回した続きも、毎回どこに聞くかを選び直してしまう
        self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, None)
        self.store.set_agent_session(req.channel, req.thread_ts, agent, "")
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
            await on_message(req, skill=skill, params=dict(params or {}))
        return True

    async def run(self, req: Request, ws: Workspace, hide: tuple[str, ...] = ()) -> runner.RunResult:
        """1回分の依頼を claude に渡し、結果をスレッドに返す。スレッドのロックを取ってから呼ぶ。

        hide に書いた頭で始まる行は、Slack に出さない（合図の行など。返す結果の本文には残す）。
        """
        assert ws.cwd is not None
        saved = list(req.saved_files)
        # 上限や再起動のあとでやり直す回にも添付を渡せるよう、保存した場所を依頼の控えに残す
        req = replace(req, saved_files=saved)
        prompt = req.text
        if saved:
            prompt += "\n\n添付ファイル（保存先）:\n" + "\n".join(f"- {p}" for p in saved)
        append_thread_log(ws.cwd, req.channel_name, req.thread_ts, WHO_BY_TRIGGER.get(req.trigger, "依頼者"),
                          req.text + ("\n\n" + "\n".join(f"- 添付: `{p}`" for p in saved) if saved else ""))

        started = time.monotonic()
        self.emit("working", theme=req.channel_name)
        self.theme_runs.begin(req.channel_name, req.thread_ts)
        before = snapshot_outputs(ws.cwd)
        run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)
        # 途中で終了させられても、次の起動で拾ってやり直せるように控えておく
        in_flight = self.store.start_in_flight(req.to_payload())
        try:
            result = await self._converse(req, ws, prompt)
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
        self.store.end_run(run_id, result.is_error, result.cost_usd, **result.recipe_fields())
        self.store.set_stalled(req.channel, req.thread_ts, req.text if result.is_error else None)

        if result.limit_reset_at is not None:
            await self.defer_for_limit(req, result.limit_reset_at, result.provider or "",
                                       mention=self._unattended(req, started))
            await self.mark_answered(req, failed=True)
            self.theme_runs.end(req.channel_name, req.thread_ts)
            return result

        # 返事待ちは、依頼者の判断を待つときだけ（❓ の確認・失敗したジョブのあと）。エラーで止まった回は
        # ⚠️ を付けるだけにする（返信すれば、止まった依頼の続きとしてやる）
        awaiting = req.awaiting_after or (not result.is_error and AWAITING_MARKER in result.text)
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        if awaiting:
            self.emit("awaiting", theme=req.channel_name)
        await self._reply(req, ws, result, hide)
        await self._attach_outputs(req, ws.cwd, before)
        await self.mark_answered(req, result.is_error)
        await self.handle_job_requests(ws.cwd)
        waiting_for_job = any(j.channel == req.channel and j.thread_ts == req.thread_ts
                              for j in self.store.active_jobs())
        if awaiting or not waiting_for_job:
            await self._notify_end(req, started, awaiting, result.is_error)
        return result

    async def _notify_end(self, req: Request, started: float, awaiting: bool, failed: bool) -> None:
        """回の終わりを、依頼者へのメンションで知らせる。返事がほしいときはいつも、待っていないはずの回は、
        終わった・止まったことを（すぐ終わった回には送らない）。様子を聞かれて答えただけの回は、何かが終わったわけでは
        ないので「終わったよ」を送らない。"""
        if awaiting:
            await self.notify_owner(req, "返事がほしいよ")
        elif self._unattended(req, started) and not (req.trigger in ("message", "voice") and is_status_inquiry(req.text)):
            await self.notify_owner(req, "止まったよ" if failed else "終わったよ")

    def _unattended(self, req: Request, started: float) -> bool:
        """依頼者が画面を見ていないはずの回か（ジョブの完了で再開した回・あとでやり直した回・長くかかった回）。

        そういう回の終わりは、依頼者へのメンションで知らせる（スレッドの投稿は、メンションが無いと通知が届かない）。
        """
        return (req.trigger in UNATTENDED_TRIGGERS or req.retried
                or time.monotonic() - started >= NOTIFY_AFTER_SECONDS)

    async def _converse(self, req: Request, ws: Workspace | None, prompt: str,
                        actor: str | None = None) -> runner.RunResult:
        """このスレッドの会話の続きとして担当の AI を動かす。会話が失われていたら、保存した会話から戻す。

        研究・自己改善は run_agent、モジュール（大学・仕事・知識など）はそのエージェントの `ask` に頼む。会話の続け方
        （session の版、履歴からの戻し、session が消えていたときのやり直し）はどの担当も同じ。
        経過はイベントとして知らせ、通知は Outbox に保存する。
        """
        # ジョブの依頼をこのスレッドのものとして確かめられるよう、先にスレッドを記録する
        req.message_ts = self.remember_message(req.channel, req.thread_ts, "owner", req.text, req.message_ts)
        row = self.store.get_thread(req.channel, req.thread_ts)
        self.store.upsert_thread(req.channel, req.thread_ts, req.channel_name, None)
        actor = actor or (themes.actor_of(ws) if ws is not None else "")
        provider = settings.selected_provider(self.config, actor)
        version = prompt_version(self.config, actor)
        session_id = self.store.session_for(req.channel, req.thread_ts, actor, provider, version)
        prior_provider = self.store.last_provider(req.channel, req.thread_ts, actor)
        # [[research-design]] などの用途の指定は依頼者の文の先頭にある（どの担当でも同じ書き方）。前置きを付ける前に読み取る
        use_case, prompt = explicit_use_case(actor, prompt)
        request_text = prompt
        # 区切って立てたスレッドの最初の回には、前のスレッドの引き継ぎメモを渡す
        prompt = handoff_memo_for(row) + prompt
        stalled = row["stalled_request"] if row else None
        if stalled or (row is not None and row["session_id"] and not session_id) or (
            prior_provider is not None and prior_provider != provider
        ):
            # 止まった回は provider 側に記録が残らないことがあるので、resume せず保存した会話から文脈を戻す
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            prompt = history_prompt(messages, prompt, req.message_ts,
                                    stalled if stalled and stalled != req.text else None, dropped=dropped)
            session_id = None
        elif session_id is None and req.message_ts and req.message_ts != req.thread_ts:
            # Kei Agent の投稿（朝の読みもの、論文の新着、朝の一覧など）への最初の返信。
            # 「2番を詳しく」に答えられるよう、元の投稿を渡す（人が始めたスレッドは今までどおり）
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            parent = next((m for m in messages if m.get("ts") == req.thread_ts), None)
            if parent is not None and parent.get("user") == "assistant":
                prompt = history_prompt(messages, prompt, req.message_ts, dropped=dropped,
                                        reply_to_post=True)

        async def on_activity(_text: str) -> None:
            self.emit("working", theme=req.channel_name)
        # 進み具合を聞かれただけの回は、読むだけで動かす（書く・コマンド・ジョブの投入ができないので、作業は始まらない）
        read_only = req.trigger in ("message", "voice") and is_status_inquiry(req.text)

        async def attempt(prompt: str, session_id: str | None) -> runner.RunResult:
            # 今日の日付と曜日は、どの担当にも同じ形で先頭に付ける（「今日の授業は？」に答えられるように）
            prompt = today_line() + prompt
            if ws is None:
                # 作業場を本体に持たない担当（モジュール）
                return await self.ask_agent(actor, prompt, session_id, req.channel, req.thread_ts,
                                            on_activity, provider=provider, use_case=use_case, read_only=read_only)
            return await self.run_agent(ws, prompt, session_id, req.channel, req.thread_ts,
                                        on_activity, provider=provider,
                                        use_case=use_case, request_text=request_text, read_only=read_only)

        result = await attempt(prompt, session_id)
        if result.session_missing:
            messages, dropped = await self.thread_messages(req.channel, req.thread_ts)
            result = await attempt(
                history_prompt(messages, prompt, req.message_ts, dropped=dropped), None)
        await self.tell_failure(actor, result)
        if not result.is_error:
            if result.session_id:
                self.store.set_session(req.channel, req.thread_ts, actor, provider,
                                       result.session_id, version)
                # threads の session_id は「このスレッドに会話がある」目印。続きの鍵は provider_sessions に置く
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
            return "研究、仕事のどれを調べるか分からなかった。"
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
        await self.notify_trouble(f"{settings.agent_labels(self.config).get(actor, actor)}の担当の AI が動きません。{how}")

    def default_question(self, actor: str) -> str:
        """メンションだけで本文が無いときに、担当に聞くこと。"""
        return getattr(self.modules.get(actor), "default_question", "") or "何ができるか教えて"

    async def converse_with_agent(self, req: Request, actor: str) -> runner.RunResult:
        """モジュールの自由な質問。研究と同じ流れ（会話の続き・経過・上限・出力の確認・再起動からのやり直し）。

        研究と違って本体に作業場を持たないので、添付の保存・スレッドのログ・出力の添付はない。
        スレッドのロックと同時実行の上限は、呼び出し側（_dispatch）が持つ。
        """
        started = time.monotonic()
        self.emit("working", theme=req.channel_name)
        run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)
        # 途中で終了させられても、次の起動で拾ってやり直せるように控えておく
        in_flight = self.store.start_in_flight(req.to_payload())
        try:
            result = await self._converse(req, None, req.text or self.default_question(actor), actor=actor)
        except asyncio.CancelledError:
            self.store.end_run(run_id, is_error=True, cost_usd=None)
            raise
        except Exception:
            self.store.finish_deferred(in_flight)
            self.store.end_run(run_id, is_error=True, cost_usd=None)
            raise
        self.store.finish_deferred(in_flight)
        self.store.end_run(run_id, result.is_error, result.cost_usd, **result.recipe_fields())
        self.store.set_stalled(req.channel, req.thread_ts, req.text if result.is_error else None)
        if result.limit_reset_at is not None:
            await self.defer_for_limit(req, result.limit_reset_at, result.provider or "",
                                       mention=self._unattended(req, started))
            await self.mark_answered(req, failed=True)
            return result
        answer, _ = self.render_reply(result)
        # 返事待ちは ❓ の確認のときだけ。エラーで止まった回は ⚠️ を付けるだけにする
        awaiting = not result.is_error and AWAITING_MARKER in result.text
        self.store.set_awaiting(req.channel, req.thread_ts, awaiting)
        for chunk in split_text(answer):
            await self.post(req, chunk, markdown=True)
        await self.mark_answered(req, result.is_error)
        await self._notify_end(req, started, awaiting, result.is_error)
        return result

    async def _reply(self, req: Request, ws: Workspace, result: runner.RunResult,
                     hide: tuple[str, ...] = ()) -> None:
        """まとめを通知に残し、テーマの会話ログにも保存する。"""
        assert ws.cwd is not None
        shown, contract_failed = self.render_reply(result)
        # 合図の行（モジュールが hide で渡したもの。着手・取り込みなど）は、検出に使うだけで Slack には出さない
        # （result.text は残す）
        shown = strip_lines(shown, hide)
        if shown:
            append_thread_log(ws.cwd, req.channel_name, req.thread_ts, "Kei Agent", shown)
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
