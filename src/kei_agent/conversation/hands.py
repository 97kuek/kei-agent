"""手の口（MCP）。頭（OpenAI Dots、今は Claude Code・Codex）から、Kei Agent の作業場で AI を動かしてもらう入口。

Slack の受け口と並ぶ、もう1つの入口（GitHub issue #17）。出す道具は3つ。

- workspaces … 頼める作業場の一覧（研究テーマ・プロジェクト・担当）。それぞれの担当・使ってよい AI・重さ
- run … 作業場・頼みごと・重さ（light / normal / deep）・AI（任意）・会話の番号（任意）で AI を動かす。
  SHORT_SECONDS のうちに終われば答えを、終わらなければ受付番号を返して裏で続ける
- status … 受付番号の作業の様子と結果

返すのは決まった項目（本文・状態・会話の番号・できたファイル）。状態は done（終わった）・needs_input（返事待ち。
本文に確認が書いてある）・failed（失敗）・accepted（受け付けた。status で見る）・running（まだ動いている）。

越えてはいけない線（秘密情報・アカウント・作業場の外・外へ送る）は、Slack から頼んだときと同じ実行の仕組みが守る。
担当・アカウント・届く範囲は作業場から決まり、頭は選べない。選べるのは、表（agents.csv の engines）で許した AI と重さだけ。
口は 127.0.0.1 で開き、合言葉（KEI_AGENT_HANDS_TOKEN）を確かめる。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, replace

from kei_agent.conversation.auto_messages import today_line
from kei_agent.conversation.response_output import OutputError, finalize_conversation
from kei_agent.conversation.slack_text import AWAITING_MARKER
from kei_agent.execution.execution_contract import prompt_version
from kei_agent.execution.runner import FAILURE_LABELS
from kei_agent.framework import modules
from kei_agent.storage import settings
from kei_agent.storage.records import Records
from kei_agent.workspaces import themes
from kei_agent.workspaces.theme_files import changed_files, snapshot_outputs
from kei_agent.workspaces.themes import ChannelKind

log = logging.getLogger(__name__)

# 会話の記録（provider のセッション）に使うチャンネルの名前。Slack のチャンネルとは混ざらない
CHANNEL = "mcp"
# この秒数のうちに終わった作業は、その場で答える。終わらなければ受付番号を返す
SHORT_SECONDS = 20
# 受付番号の結果を残す日数
KEEP_DAYS = 7
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
        """頼める作業場。研究テーマ・プロジェクト（作業場のフォルダがあるもの）と、担当（大学・仕事・知識など）。"""
        found: dict[str, dict] = {}
        for name in themes.all_themes(self.config):
            found[name] = self._describe(name)
        for name in self._projects():
            found[name] = self._describe(name)
        for spec in modules.enabled(self.config.modules):
            if spec.actor is None or spec.catch_all or spec.prefixes or not spec.channels:
                continue
            names = self.config.module_channels.get(next(iter(spec.channels)), ())
            if names:
                found[names[0]] = self._describe(names[0])
        return [item for item in found.values() if item]

    def _projects(self) -> list[str]:
        """プロジェクトのチャンネルの名前（作業場のフォルダの下にあるものと、themes.toml の既存のフォルダ）。"""
        from kei_agent.configuration.places import places

        names = [name for name in places(self.config) if self._kind(name) is ChannelKind.PROJECT]
        for spec in modules.enabled(self.config.modules):
            for head in spec.prefixes:
                root = self.config.module_workspace(spec.name)
                if root.is_dir():
                    names += [f"{head}{p.name}" for p in sorted(root.iterdir())
                              if p.is_dir() and not p.name.startswith(".")]
        return list(dict.fromkeys(names))

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
        conversation = conversation or f"c-{secrets.token_hex(6)}"
        ticket = f"t-{secrets.token_hex(6)}"
        self.records.put("ticket", ticket, {"ticket": ticket, "status": "running", "workspace": plan.ws.channel_name,
                                            "conversation": conversation, "started_at": time.time()},
                         keep_days=KEEP_DAYS)
        task = asyncio.create_task(self._work(ticket, plan, request, conversation))
        self._tasks[ticket] = task
        task.add_done_callback(lambda _: self._tasks.pop(ticket, None))
        done, _ = await asyncio.wait({task}, timeout=SHORT_SECONDS)
        if done:
            return task.result()
        return {"status": "accepted", "ticket": ticket, "conversation": conversation,
                "text": "受け付けました。終わったら status で結果を見てください"}

    def _plan(self, workspace: str, request: str, weight: str, engine: str, use_case: str = "",
              read_only: bool = False, minutes: int = 0) -> Plan:
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
        if (until := self.assistant.store.limit_until(provider)) > time.time():
            raise HandsError(f"{provider} は利用上限で止まっています（{_clock(until)} ごろに明ける）。"
                             "ほかの AI が選べれば engine で選んでください")
        names = {u.name for u in spec.actor.use_cases}
        if use_case and use_case not in names:
            raise HandsError(f"{workspace} の用途は {' / '.join(sorted(names))} のどれかです（workspaces の use_cases）")
        top = self._max_minutes(ws, spec)
        if minutes and not 1 <= minutes <= top:
            raise HandsError(f"minutes は 1〜{top} 分にしてください")
        return Plan(ws, provider, use_case or spec.actor.use_case_for(weight), bool(read_only), minutes or None)

    async def _work(self, ticket: str, plan: Plan, request: str, conversation: str) -> dict:
        """作業をして、結果を受付番号の記録に残す。どこで失敗しても、記録は failed にする（running のまま残さない）。"""
        try:
            # 同じ会話の続きは1つずつ（同時に動かすと、同じ会話が枝分かれして片方が消える）
            async with self.assistant.thread_locks[(CHANNEL, conversation)]:
                out = await self._run_once(plan, request, conversation)
        except Exception:
            log.exception("手の口の作業に失敗しました")
            out = {"status": "failed", "text": "作業に失敗しました（くわしくは Kei Agent のログ）",
                   "conversation": conversation, "files": []}
        self.records.update("ticket", ticket, **out)
        return {**out, "ticket": ticket}

    async def _run_once(self, plan: Plan, request: str, conversation: str) -> dict:
        assistant, store = self.assistant, self.assistant.store
        ws, provider = plan.ws, plan.provider
        actor = ws.module
        version = prompt_version(self.config, actor, for_head=True)
        run_id = store.start_run(CHANNEL, conversation, ws.channel_name, "mcp")
        # 会話を作業場に結び付けておく（AI が頼んだ研究のジョブを、この作業場のものとして受け付けるため）
        store.upsert_thread(CHANNEL, conversation, ws.channel_name, None)
        result = None
        before: dict = {}
        try:
            session_id = store.session_for(CHANNEL, conversation, actor, provider, version)
            async with assistant.semaphore:
                if ws.kind in WORKSPACE_KINDS:
                    themes.ensure_workspace(ws)
                    before = snapshot_outputs(ws.cwd) if ws.cwd is not None else {}
                result = await self._attempt(replace(plan, ws=ws), today_line() + request, session_id, conversation)
                if result.session_missing:
                    result = await self._attempt(replace(plan, ws=ws), today_line() + request, None, conversation)
        finally:
            store.end_run(run_id, result is None or result.is_error, result.cost_usd if result else None,
                          **(result.recipe_fields() if result else {}))
        if result.limit_reset_at is not None and result.provider:
            # 上限に当たった。明けるまで、その provider を頼まない（Slack・定期処理とも同じ記録）
            store.set_limit_until(result.provider, max(store.limit_until(result.provider),
                                                       assistant.limit_until(result.limit_reset_at)))
        if not result.is_error and result.session_id:
            store.set_session(CHANNEL, conversation, actor, provider, result.session_id, version)
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

    # 様子

    def status(self, ticket: str) -> dict:
        found = self.records.get("ticket", ticket)
        if found is None:
            raise HandsError(f"受付番号 {ticket} が見つかりません（{KEEP_DAYS}日より前のものは消えます）")
        if found.get("status") == "running" and ticket not in self._tasks:
            # 本体が起動し直して、作業が途中で止まった
            found = {**found, "status": "failed", "text": "Kei Agent が起動し直したので、作業が途中で止まりました。もう一度頼んでください"}
        return {**{k: v for k, v in found.items() if k != "started_at"}, "ticket": ticket}


def _clock(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


def _status(result) -> str:
    if result.is_error:
        return "failed"
    return "needs_input" if AWAITING_MARKER in (result.text or "") else "done"


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
        # 決まった形でなくても、頭には中身をそのまま見せる（Slack に出すわけではない）
        return (result.text or "").strip()
