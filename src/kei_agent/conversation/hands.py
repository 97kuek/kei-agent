"""MCP の作業実行。Dot・Claude Code・Codex から作業場の AI を呼び出す。

workspaces で担当と実行設定を確認し、run で作業を受け付け、status で結果を取得する。
待機・実行・完了は phase、受付からの経過は elapsed_seconds で返す。
通知は Outbox に保存し、Dot が notices で取得して Slack に投稿する。

担当・アカウント・権限は作業場から決まる。既存の実行制限はすべて適用する。
MCP サーバーは 127.0.0.1 で動き、KEI_AGENT_HANDS_TOKEN で認証する。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass

from kei_agent.conversation.auto_messages import HANDOFF_MEMO_PROMPT, handoff_start_prompt, history_prompt, today_line
from kei_agent.conversation.response_output import OutputError, finalize_conversation, safe_failure
from kei_agent.conversation.slack_text import AWAITING_MARKER
from kei_agent.execution.execution_contract import prompt_version
from kei_agent.execution.runner import FAILURE_LABELS
from kei_agent.framework import modules
from kei_agent.storage import settings
from kei_agent.storage.records import Records
from kei_agent.workspaces import themes
from kei_agent.workspaces.theme_files import (
    append_thread_log,
    changed_files,
    free_name,
    safe_filename,
    snapshot_outputs,
    thread_log_path,
)
from kei_agent.workspaces.themes import ChannelKind

log = logging.getLogger(__name__)

# 会話の記録（provider のセッション）に使うチャンネルの名前。Slack のチャンネルとは混ざらない
CHANNEL = "mcp"
# この秒数のうちに終わった作業は、その場で答える。終わらなければ受付番号を返す
SHORT_SECONDS = 20
# 受付番号の結果を残す日数
KEEP_DAYS = 7
# 頭が渡す会話の番号（Slack のスレッドの ts など）。作業場の記録のファイル名にもなるので、文字と長さを絞る
CONVERSATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# read_file で返す長さと、put_file で受け取る長さ
FILE_CHARS = 100_000
# 作業場を本体に持つ種類（研究テーマ・プロジェクト・研究全体）。ほかは担当のプロセスに頼む
WORKSPACE_KINDS = (ChannelKind.THEME, ChannelKind.PROJECT, ChannelKind.OVERVIEW)


@dataclass(frozen=True)
class Plan:
    """受け付けた頼みごとの中身（作業場・AI・用途・読むだけ・上限時間）。"""
    ws: object
    provider: str
    use_case: str
    read_only: bool = False
    minutes: int | None = None


class HandsError(ValueError):
    """頼みごとを受け付けられない（作業場が無い、使えない AI、など）。理由は本文。"""


class Hands:
    def __init__(self, assistant):
        self.assistant = assistant
        self.records = Records(assistant.store, "hands")
        self._tasks = assistant.hands_tasks

    @property
    def config(self):
        return self.assistant.config

    # 作業場

    def workspaces(self) -> list[dict]:
        """頼める作業場。研究テーマ・プロジェクト（作業場のフォルダがあるもの）と、担当（仕事など）。"""
        found: dict[str, dict] = {}
        for name in themes.all_themes(self.config):
            found[name] = self._describe(name)
        for name in self._projects():
            found[name] = self._describe(name)
        for spec in modules.enabled(self.config.modules):
            if spec.actor is None or spec.catch_all:
                continue
            # 担当のチャンネル（頭が一致するプロジェクトのチャンネルは、上の作業場として並べる）
            names = [name for kind in spec.channels for name in self.config.module_channels.get(kind, ())
                     if not modules.channel_prefix(name)]
            if names:
                found[names[0]] = self._describe(names[0])
        return [item for item in found.values() if item]

    def _projects(self) -> list[str]:
        """プロジェクトのチャンネルの名前（作業場のフォルダの下にあるものと、themes.toml の既存のフォルダ）。"""
        return list(themes.all_projects(self.config))

    def _kind(self, name: str) -> ChannelKind | None:
        try:
            return themes.resolve(self.config, name).kind
        except ValueError:
            return None

    def _describe(self, name: str) -> dict:
        ws = themes.resolve(self.config, name)
        spec = modules.known().get(ws.module)
        if spec is None or spec.actor is None:
            return {}
        what = {ChannelKind.THEME: "研究テーマ", ChannelKind.PROJECT: "プロジェクト"}.get(ws.kind, "担当")
        return {"name": ws.channel_name, "kind": what, "agent": spec.label,
                "engines": list(self.config.agent_profiles[ws.module].allowed_engines),
                "weights": list(modules.WEIGHTS),
                # 重さの代わりに名前で選べる用途（manual は、いちばん強いモデルなど、名前でだけ選ぶもの）
                "use_cases": [{"name": u.name, "manual": u.manual} for u in spec.actor.use_cases],
                "max_minutes": self._max_minutes(ws, spec)}

    def _max_minutes(self, ws, spec) -> int:
        """頭が決められる上限時間（分）。担当の上限と config.toml の上限の、長いほう。"""
        own = ws.timeout_minutes or spec.actor.timeout_minutes or self.config.run_timeout_minutes
        return max(own, self.config.run_timeout_minutes)

    # 頼む

    async def run(self, workspace: str, request: str, weight: str = "normal", engine: str = "",
                  conversation: str = "", use_case: str = "", read_only: bool = False,
                  minutes: int = 0) -> dict:
        """作業場で AI を動かす。短ければ答えを、長ければ受付番号を返す。受け付けられなければ HandsError。

        use_case を渡せば重さより先に使う（workspaces の use_cases の名前）。read_only なら、書く・動かす・通信する
        手段を外す。minutes は上限時間（workspaces の max_minutes まで。0 なら担当の既定）。
        """
        plan = self._plan(workspace, request, weight, engine, use_case, read_only, minutes)
        if conversation and (not CONVERSATION.fullmatch(conversation) or ".." in conversation):
            raise HandsError("conversation は英数字と . _ - だけの64字までにしてください（Slack のスレッドの ts など）")
        conversation = conversation or f"c-{secrets.token_hex(6)}"
        row = self.assistant.store.get_thread(CHANNEL, conversation)
        if row is not None and row["channel_name"] != plan.ws.channel_name:
            raise HandsError("conversation は別の作業場の会話です。新しい会話として頼んでください")
        # 受付時に結び付ける。待機中の同じ番号を別の作業場で使わせない。
        self.assistant.store.upsert_thread(CHANNEL, conversation, plan.ws.channel_name, None)
        ticket = f"t-{secrets.token_hex(6)}"
        self.records.put("ticket", ticket, {"ticket": ticket, "status": "running", "phase": "queued", "workspace": plan.ws.channel_name,
                                            "conversation": conversation, "started_at": time.time()},
                         keep_days=KEEP_DAYS)
        task = asyncio.create_task(self._work(ticket, plan, request, conversation))
        self._tasks[ticket] = task
        task.add_done_callback(lambda _: self._tasks.pop(ticket, None))
        return await self._wait_ticket(ticket, task, conversation)

    async def _wait_ticket(self, ticket: str, task: asyncio.Task, conversation: str) -> dict:
        done, _ = await asyncio.wait({task}, timeout=SHORT_SECONDS)
        if done:
            task.result()
            return self.status(ticket)
        progress = self.status(ticket)
        return {"status": "accepted", "ticket": ticket, "conversation": conversation,
                "phase": progress["phase"], "elapsed_seconds": progress["elapsed_seconds"],
                "text": "受け付けました。終わったら status で結果を見てください"}

    async def handoff(self, workspace: str, conversation: str) -> dict:
        """既存の会話を読むだけでまとめ、新しい会話とメモを返す。中断時は手動で頼み直す。"""
        if not CONVERSATION.fullmatch(conversation) or ".." in conversation:
            raise HandsError("conversation は英数字と . _ - だけの64字までにしてください")
        row = self.assistant.store.get_thread(CHANNEL, conversation)
        if row is None:
            raise HandsError("conversation の会話が見つかりません")
        completed = self.records.get("handoff", conversation)
        plan = self._plan(workspace, HANDOFF_MEMO_PROMPT, "light", "", read_only=True,
                          allow_limited=completed is not None)
        if row["channel_name"] != plan.ws.channel_name:
            raise HandsError("conversation は別の作業場の会話です")
        for ticket in self.records.items("ticket"):
            if (ticket.get("operation") == "handoff" and ticket.get("source_conversation") == conversation
                    and ticket["workspace"] == plan.ws.channel_name and ticket.get("status") == "running"
                    and (task := self._tasks.get(ticket["ticket"])) is not None):
                return await self._wait_ticket(ticket["ticket"], task, conversation)
        ticket = f"t-{secrets.token_hex(6)}"
        self.records.put("ticket", ticket, {
            "ticket": ticket, "operation": "handoff", "status": "running", "phase": "queued",
            "workspace": plan.ws.channel_name, "source_conversation": conversation, "conversation": conversation,
            "started_at": time.time(),
        }, keep_days=KEEP_DAYS)
        task = asyncio.create_task(self._work(ticket, plan, HANDOFF_MEMO_PROMPT, conversation, handoff=True))
        self._tasks[ticket] = task
        task.add_done_callback(lambda _: self._tasks.pop(ticket, None))
        return await self._wait_ticket(ticket, task, conversation)

    def _plan(self, workspace: str, request: str, weight: str, engine: str, use_case: str = "",
              read_only: bool = False, minutes: int = 0, *, allow_limited: bool = False) -> Plan:
        if not request.strip():
            raise HandsError("頼みごとが空です")
        if weight not in modules.WEIGHTS:
            raise HandsError(f"重さは {' / '.join(modules.WEIGHTS)} のどれかにしてください")
        try:
            ws = themes.resolve(self.config, workspace)
        except ValueError as e:
            raise HandsError(str(e)) from None
        spec = modules.known().get(ws.module)
        if ws.kind not in (*WORKSPACE_KINDS, ChannelKind.MODULE) or spec is None or spec.actor is None:
            raise HandsError(f"{workspace} には頼めません（workspaces で頼める作業場を見てください）")
        if ws.module not in self.assistant.modules:
            raise HandsError(f"{workspace} を受け持つ担当がオフです")
        if ws.kind in (ChannelKind.THEME, ChannelKind.PROJECT) and (ws.cwd is None or not ws.cwd.is_dir()):
            # 打ち間違いで新しいテーマやプロジェクトのフォルダを作らない（作るのは Slack でチャンネルを作ったとき）
            raise HandsError(f"{workspace} という作業場はありません（workspaces で頼める作業場を見てください）")
        profile = self.config.agent_profiles[ws.module]
        provider = engine or settings.selected_provider(self.config, ws.module)
        if provider not in profile.allowed_engines:
            raise HandsError(f"{workspace} で使える AI は {' / '.join(profile.allowed_engines) or 'まだ選ばれていません'}です"
                             "（agents.csv の engine・engines）")
        if (until := self.assistant.store.limit_until(provider)) > time.time() and not allow_limited:
            raise HandsError(f"{provider} は利用上限で止まっています（{_clock(until)} ごろに明ける）。"
                             "ほかの AI が選べれば engine で選んでください")
        names = {u.name for u in spec.actor.use_cases}
        if use_case and use_case not in names:
            raise HandsError(f"{workspace} の用途は {' / '.join(sorted(names))} のどれかです（workspaces の use_cases）")
        top = self._max_minutes(ws, spec)
        if minutes and not 1 <= minutes <= top:
            raise HandsError(f"minutes は 1〜{top} 分にしてください")
        return Plan(ws, provider, use_case or spec.actor.use_case_for(weight), bool(read_only), minutes or None)

    async def _work(self, ticket: str, plan: Plan, request: str, conversation: str, *, handoff: bool = False) -> dict:
        """作業をして、結果を受付番号の記録に残す。どこで失敗しても、記録は failed にする（running のまま残さない）。"""
        try:
            # 同じ会話の続きは1つずつ（同時に動かすと、同じ会話が枝分かれして片方が消える）
            async with self.assistant.thread_locks[(CHANNEL, conversation)], self.assistant.semaphore:
                self.records.update("ticket", ticket, phase="running")
                self.assistant.emit("working", theme=plan.ws.channel_name)
                out = (await self._handoff_once(plan, conversation) if handoff
                       else await self._run_once(plan, request, conversation))
        except asyncio.CancelledError:
            self.records.update("ticket", ticket, status="failed", phase="failed", finished_at=time.time(),
                                text="作業が中断されました。もう一度頼んでください")
            self.assistant.emit("failed", theme=plan.ws.channel_name)
            raise
        except Exception:
            log.exception("MCP の作業に失敗しました")
            out = {"status": "failed", "text": "作業に失敗しました（くわしくは Kei Agent のログ）",
                   "conversation": conversation, "files": []}
        self.records.update("ticket", ticket, **out, phase=out["status"], finished_at=time.time())
        event = {"done": "done", "needs_input": "awaiting", "failed": "failed"}[out["status"]]
        self.assistant.emit(event, theme=plan.ws.channel_name)
        return {**out, "ticket": ticket}

    async def _handoff_once(self, plan: Plan, conversation: str) -> dict:
        store = self.assistant.store
        row = store.get_thread(CHANNEL, conversation)
        if row is None or row["channel_name"] != plan.ws.channel_name:
            raise HandsError("conversation の作業場が一致しません")
        completed = self.records.get("handoff", conversation)
        if completed is None:
            if row["handed_off_to"]:
                raise HandsError("引き継ぎの記録が揃っていません。会話の記録を確認してください")
            # 受付時には空いていても、会話や同時実行枠を待つ間に上限へ達することがある。
            if (until := store.limit_until(plan.provider)) > time.time():
                return {"status": "failed", "text": f"{plan.provider} は利用上限で止まっています（{_clock(until)} ごろに明ける）",
                        "conversation": conversation, "files": []}
            messages, _ = await self.assistant.thread_messages(CHANNEL, conversation)
            if not messages:
                raise HandsError("引き継ぐ会話の記録がありません")
            result = await self._run_once(plan, HANDOFF_MEMO_PROMPT, conversation, handoff=True)
            if result["status"] != "done":
                return result
            completed = {**result, "memo": result["text"], "workspace": plan.ws.channel_name,
                         "source_conversation": conversation, "conversation": f"c-{secrets.token_hex(6)}"}
            # 完成したメモと新会話番号を1件として保存する。ここから先で起動が止まっても同じ番号へ復元できる。
            self.records.put("handoff", conversation, completed)
        if completed["workspace"] != plan.ws.channel_name:
            raise HandsError("引き継ぎの作業場が一致しません")
        new_conversation = completed["conversation"]
        new_row = store.get_thread(CHANNEL, new_conversation)
        if new_row is not None and new_row["channel_name"] != plan.ws.channel_name:
            raise HandsError("新しい会話は別の作業場にあります")
        store.upsert_thread(CHANNEL, new_conversation, plan.ws.channel_name, None)
        previous_log = ""
        if plan.ws.cwd is not None:
            previous_log = str(thread_log_path(plan.ws.cwd, conversation).relative_to(plan.ws.cwd))
        store.update_thread(CHANNEL, new_conversation,
                            handoff_memo=handoff_start_prompt(completed["memo"], previous_log))
        store.update_thread(CHANNEL, conversation, handed_off_to=new_conversation)
        store.set_awaiting(CHANNEL, conversation, False)
        if new_row is None and plan.ws.cwd is not None:
            append_thread_log(plan.ws.cwd, plan.ws.channel_name, new_conversation, "Kei Agent（引き継ぎ）",
                              completed["memo"])
        return completed

    async def _run_once(self, plan: Plan, request: str, conversation: str, *, handoff: bool = False) -> dict:
        assistant, store = self.assistant, self.assistant.store
        ws, provider = plan.ws, plan.provider
        actor = ws.module
        # 作業場ごとの指示書（テーマの AGENTS.md など）も版に入れる（変えたら古い会話を続けない）
        version = prompt_version(self.config, actor, ws if ws.kind in WORKSPACE_KINDS else None, for_head=True)
        row = store.get_thread(CHANNEL, conversation)
        run_id = store.start_run(CHANNEL, conversation, ws.channel_name, "handoff" if handoff else "mcp")
        # 会話を作業場に結び付けておく（AI が頼んだ研究のジョブを、この作業場のものとして受け付けるため）
        store.upsert_thread(CHANNEL, conversation, ws.channel_name, None)
        result = None
        before: dict = {}
        try:
            session_id = store.session_for(CHANNEL, conversation, actor, provider, version)
            if ws.kind in WORKSPACE_KINDS:
                themes.ensure_workspace(ws)
                before = snapshot_outputs(ws.cwd) if ws.cwd is not None else {}
            if ws.cwd is not None:
                append_thread_log(ws.cwd, ws.channel_name, conversation, "依頼者", request)
            request_ts = None if handoff else assistant.remember_message(CHANNEL, conversation, "owner", request)
            messages, dropped = await assistant.thread_messages(CHANNEL, conversation)
            premise = (row["handoff_memo"] or "") if row is not None else ""
            next_request = (premise if session_id is None else "") + request
            restored = history_prompt(messages, premise + request, request_ts, dropped=dropped)
            has_history = any(m.get("ts") != request_ts for m in messages)
            prompt = today_line() + (restored if session_id is None and has_history else next_request)
            result = await self._attempt(plan, prompt, session_id, conversation)
            if result.session_missing:
                result = await self._attempt(plan, today_line() + restored, None, conversation)
        finally:
            store.end_run(run_id, result is None or _status(result) == "failed", result.cost_usd if result else None,
                          **(result.recipe_fields() if result else {}))
        if result.limit_reset_at is not None and result.provider:
            # 上限に当たった。明けるまで、その provider を頼まない（Slack・定期処理とも同じ記録）
            store.set_limit_until(result.provider, max(store.limit_until(result.provider),
                                                       assistant.limit_until(result.limit_reset_at)))
            assistant.emit("limited", reset_at=time.strftime("%Y-%m-%dT%H:%M", time.localtime(
                store.limit_until(result.provider))))
        if _status(result) != "failed" and result.session_id:
            store.set_session(CHANNEL, conversation, actor, provider, result.session_id, version)
        if handoff and not result.is_error:
            try:
                finalize_conversation(result.text)
            except OutputError:
                return {"status": "failed", "text": "引き継ぎメモを利用者向けの形に整えられませんでした。もう一度頼んでください",
                        "conversation": conversation, "files": []}
        if shown := _text(result):
            assistant.remember_message(CHANNEL, conversation, "assistant", shown)
        if ws.kind in WORKSPACE_KINDS and ws.cwd is not None and (shown := _text(result)):
            append_thread_log(ws.cwd, ws.channel_name, conversation, "Kei Agent", shown)
        files = []
        if ws.kind in WORKSPACE_KINDS and ws.cwd is not None:
            for path in changed_files(before, snapshot_outputs(ws.cwd)):
                with suppress(OSError, ValueError):
                    files.append({"path": str(path.relative_to(ws.cwd)), "bytes": path.stat().st_size})
        return {"status": _status(result), "text": _text(result), "conversation": conversation, "files": files,
                "engine": result.provider or provider, "model": result.model or ""}

    async def _attempt(self, plan: Plan, prompt: str, session_id: str | None, conversation: str):
        options = {"provider": plan.provider, "use_case": plan.use_case, "read_only": plan.read_only,
                   "for_head": True, "timeout_minutes": plan.minutes}
        if plan.ws.kind in WORKSPACE_KINDS:
            return await self.assistant.run_agent(plan.ws, prompt, session_id, CHANNEL, conversation, **options)
        return await self.assistant.ask_agent(plan.ws.module, prompt, session_id, CHANNEL, conversation, **options)

    # 作業場を作る（Slack でチャンネルに Kei Agent を招いたときと同じ。研究テーマとプロジェクト）

    async def create_workspace(self, name: str, folder: str = "") -> dict:
        """研究テーマかプロジェクトの作業場を作る。folder を渡すと、既存のフォルダを使う（themes.toml に書く）。
        研究テーマは研究ホームにも登録する。もうあれば作らずに、その場所を返す。"""
        from kei_agent.configuration.places import PlaceError

        name = themes.theme_name(name.strip())
        try:
            ws = themes.resolve(self.config, name)
            if ws.kind not in (ChannelKind.THEME, ChannelKind.PROJECT):
                raise HandsError(f"{name} は研究テーマやプロジェクトの名前ではありません（担当のチャンネルの名前です）")
            if folder.strip() and (ws.cwd is None or not ws.cwd.exists()):
                themes.save_place(self.config, name, themes.check_place(self.config, folder.strip()))
                ws = themes.resolve(self.config, name)
        except (ValueError, PlaceError) as e:
            raise HandsError(str(e)) from None
        if ws.cwd is None:
            raise HandsError(f"{name} の作業場の場所が決まりません")
        created = themes.ensure_workspace(ws)
        if ws.kind is ChannelKind.THEME:
            self.assistant.registered_themes.add(ws.channel_name)
            await self.assistant.register_theme("", ws)
        return {"name": ws.channel_name, "kind": "研究テーマ" if ws.kind is ChannelKind.THEME else "プロジェクト",
                "folder": str(ws.cwd), "created": created}

    # 時間を測る（Slack の /toggl とカードの代わり。時間記録のモジュール）

    async def timer(self, action: str, domain: str = "", label: str = "", entry_id: str = "",
                    memo: str = "", resolution: str = "") -> dict:
        """計測を始める（前の計測は止める）・止める・今の様子。止めた記録は Toggl と共通ホームの「時間記録」へ送る。"""
        if action not in ("start", "stop", "status", "memo", "resolve"):
            raise HandsError("action は start / stop / status / memo / resolve のどれかにしてください")
        try:
            return await self.assistant.module_head_action("timer", {"action": action, "domain": domain, "label": label,
                                                                    "entry_id": entry_id, "memo": memo,
                                                                    "resolution": resolution})
        except ValueError as e:
            raise HandsError(str(e)) from None

    # Moodle の提出・受験終了（大学のモジュール）

    async def sync_submissions(self) -> dict:
        """大学モジュールに、Moodle の提出・受験状態の機械的な同期を頼む。"""
        try:
            return await self.assistant.module_head_action("sync_submissions", {})
        except ValueError as error:
            raise HandsError(str(error)) from None

    # Mac の音声通知とマイクの設定（声のモジュール）

    async def voice(self, notify: bool | None = None, listen: bool | None = None) -> dict:
        """声のスイッチを変える（渡さなかったほうは変えない）。今の2つのスイッチを返す。"""
        try:
            return await self.assistant.module_head_action("voice", {"notify": notify, "listen": listen})
        except ValueError as e:
            raise HandsError(str(e)) from None

    async def save_reading(self, url: str, saved: bool = True) -> dict:
        """Mac の旧配信分の読みものを保存・解除する互換窓口。"""
        try:
            return await self.assistant.module_head_action("save_reading", {"url": url, "saved": saved})
        except ValueError as error:
            raise HandsError(str(error)) from None

    # ファイル（Slack の添付の代わり。頭が渡したものは inputs/ に置き、作業でできた outputs/ のものを読む）

    def _folder(self, workspace: str):
        try:
            ws = themes.resolve(self.config, workspace)
        except ValueError as e:
            raise HandsError(str(e)) from None
        if ws.kind not in (ChannelKind.THEME, ChannelKind.PROJECT) or ws.cwd is None or not ws.cwd.is_dir():
            raise HandsError(f"{workspace} にはファイルを置けません（研究テーマとプロジェクトの作業場だけ）")
        return ws.cwd.resolve()

    def put_file(self, workspace: str, name: str, content: str) -> dict:
        """頭が渡した文のファイルを、作業場の inputs/ に置く。run の頼みごとで、そのパスを伝える。"""
        folder = self._folder(workspace)
        if "/" in name or "\\" in name or not name.strip():
            raise HandsError("ファイルの名前は、フォルダを含まない名前にしてください")
        if len(content) > FILE_CHARS:
            raise HandsError(f"ファイルは {FILE_CHARS} 字までです")
        inputs = folder / "inputs"
        inputs.mkdir(exist_ok=True)
        # Slack の添付と同じ決まり（使えない文字は _ に、同じ名前があれば番号を足す。前のものは上書きしない）
        dest = free_name(inputs, safe_filename(name))
        dest.write_text(content, encoding="utf-8")
        return {"path": str(dest.relative_to(folder))}

    def read_file(self, workspace: str, path: str) -> dict:
        """作業場の outputs/ にできたファイルの中身（run の files に出たもの。文のファイルだけ）。"""
        folder = self._folder(workspace)
        target = (folder / path).resolve()
        if not target.is_relative_to(folder / "outputs") or not target.is_file():
            raise HandsError(f"{path} は読めません（作業場の outputs/ にあるファイルだけ）")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HandsError(f"{path} は文のファイルではないので読めません（作業場で開いてください）") from None
        return {"path": str(target.relative_to(folder)), "text": text[:FILE_CHARS], "truncated": len(text) > FILE_CHARS}

    # 様子

    # Dot が取得する通知

    def notices(self, done: list[str] | None = None) -> dict:
        """まだ出していない知らせ（古い順）。done に渡した id は「出した」にして、書き換えられない限り次からは返さない
        （Dot が Slack に出せたものだけを done で返す。途中で切れても知らせは消えない）。読むだけでは確認済みにしない。
        """
        outbox = self.assistant.slack
        outbox.mark_delivered([str(i) for i in done or []])
        found = outbox.pending()
        return {"notices": [{k: item[k] for k in ("id", "channel", "thread_ts", "thread", "text")}
                            | {"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(item["at"]))}
                            for item in found], "slack": False}

    def status(self, ticket: str) -> dict:
        found = self.records.get("ticket", ticket)
        if found is None:
            raise HandsError(f"受付番号 {ticket} が見つかりません（{KEEP_DAYS}日より前のものは消えます）")
        if found.get("status") == "running" and ticket not in self._tasks:
            # 本体が起動し直して、作業が途中で止まった
            self.records.update("ticket", ticket, status="failed", phase="failed", finished_at=time.time(),
                                text="Kei Agent が起動し直したので、作業が途中で止まりました。もう一度頼んでください")
            found = self.records.get("ticket", ticket)
        finished = found.get("finished_at", time.time() if found["status"] == "running" else found["started_at"])
        return {**{k: v for k, v in found.items() if k not in {"started_at", "finished_at"}}, "ticket": ticket,
                "phase": found.get("phase", found["status"]),
                "elapsed_seconds": max(0, int(finished - found["started_at"]))}


def _clock(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


def _status(result) -> str:
    if result.is_error:
        return "failed"
    try:
        shown = finalize_conversation(result.text)
    except OutputError:
        return "failed"
    # 確認の合図は最後の行だけを見る（本文の途中に引用された合図では止めない）
    last = next((line.strip() for line in reversed(shown.splitlines()) if line.strip()), "")
    return "needs_input" if last.startswith(AWAITING_MARKER) else "done"


def _text(result) -> str:
    """頭に見せる本文。AI の答えのうち、見せる部分（final の印の間）。失敗なら理由の短い文。"""
    if result.is_error:
        # 頭（外のサービス）には、決まった短い理由だけを返す（エラーの中身にはパスなどが入りうる。ログに残る）
        if result.limit_reset_at is not None:
            when = f"（{_clock(result.limit_reset_at)} ごろに明ける）" if result.limit_reset_at > time.time() else ""
            return f"利用上限に当たりました{when}"
        kind = FAILURE_LABELS.get(result.failure_kind or "", "")
        return f"作業に失敗しました（{kind}）" if kind else "作業に失敗しました（AI が答えませんでした）"
    try:
        return finalize_conversation(result.text)
    except OutputError:
        return safe_failure("conversation")
