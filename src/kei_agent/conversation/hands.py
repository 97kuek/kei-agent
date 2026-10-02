"""手の口（MCP）。頭（OpenAI Dots、今は Claude Code・Codex）から、Kei Agent の作業場で AI を動かしてもらう入口。

Slack の受け口と並ぶ、もう1つの入口（GitHub issue #17）。AI を動かす道具は3つと、投稿の道具が1つ。

- workspaces … 頼める作業場の一覧（研究テーマ・プロジェクト・担当）。それぞれの担当・使ってよい AI・重さ
- run … 作業場・頼みごと・重さ（light / normal / deep）・AI（任意）・会話の番号（任意）で AI を動かす。
  SHORT_SECONDS のうちに終われば答えを、終わらなければ受付番号を返して裏で続ける
- status … 受付番号の作業の様子と結果
- post … 研究全体のチャンネルに、Kei Agent の名前で投稿する（頭の予定で動かす Daily など）。ほかのチャンネルには出せない。
  Slack につないでいないときは断る（頭が自分の名前で出す）
- notices … Slack につないでいないとき、本体とモジュールが Slack に出すつもりだった知らせ（ジョブが終わった・
  困りごと・課題の新着など。conversation/outbox.py）。頭が読んで、自分の名前で Slack に出す

返すのは決まった項目（本文・状態・会話の番号・できたファイル）。状態は done（終わった）・needs_input（返事待ち。
本文に確認が書いてある）・failed（失敗）・accepted（受け付けた。status で見る）・running（まだ動いている）。

越えてはいけない線（秘密情報・アカウント・作業場の外・外へ送る）は、Slack から頼んだときと同じ実行の仕組みが守る。
担当・アカウント・届く範囲は作業場から決まり、頭は選べない。選べるのは、表（agents.csv の engines）で許した AI と重さだけ。
口は 127.0.0.1 で開き、合言葉（KEI_AGENT_HANDS_TOKEN）を確かめる。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, replace

from kei_agent.conversation.auto_messages import today_line
from kei_agent.conversation.response_output import OutputError, finalize_conversation
from kei_agent.conversation.slack_text import AWAITING_MARKER, split_text
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
        """頼める作業場。研究テーマ・プロジェクト（作業場のフォルダがあるもの）と、担当（大学・仕事・知識など）。"""
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
        if conversation and (not CONVERSATION.fullmatch(conversation) or ".." in conversation):
            raise HandsError("conversation は英数字と . _ - だけの64字までにしてください（Slack のスレッドの ts など）")
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
        # 作業場ごとの指示書（テーマの AGENTS.md など）も版に入れる（変えたら古い会話を続けない）
        version = prompt_version(self.config, actor, ws if ws.kind in WORKSPACE_KINDS else None, for_head=True)
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
                if ws.cwd is not None:
                    # やり取りを作業場に残す（Slack の受け口と同じ置き場所。会話の番号ごとに1ファイル）
                    append_thread_log(ws.cwd, ws.channel_name, conversation, "依頼者", request)
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

    async def timer(self, action: str, domain: str = "", label: str = "") -> dict:
        """計測を始める（前の計測は止める）・止める・今の様子。止めた記録は Toggl と共通ホームの「時間記録」へ送る。"""
        if action not in ("start", "stop", "status"):
            raise HandsError("action は start / stop / status のどれかにしてください")
        try:
            return await self.assistant.module_head_action("timer", {"action": action, "domain": domain, "label": label})
        except ValueError as e:
            raise HandsError(str(e)) from None

    # 声（App Home の「知らせる」「聞く（マイク）」の代わり。声のモジュール）

    async def voice(self, notify: bool | None = None, listen: bool | None = None) -> dict:
        """声のスイッチを変える（渡さなかったほうは変えない）。今の2つのスイッチを返す。"""
        try:
            return await self.assistant.module_head_action("voice", {"notify": notify, "listen": listen})
        except ValueError as e:
            raise HandsError(str(e)) from None

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

    # 研究全体のチャンネルへの投稿（頭の予定で動かす Daily などを、Kei Agent の名前で出す）

    async def post(self, text: str, details: str = "") -> dict:
        """研究全体のチャンネルに text を出し、details があればそのスレッドに出す（どちらも Markdown）。
        出せるのは研究全体のチャンネルだけ（頭はチャンネルを選べない）。"""
        if not text.strip():
            raise HandsError("投稿する本文が空です")
        if self._outbox() is not None:
            raise HandsError("Kei Agent は Slack につないでいません。Slack には自分の Slack 連携で出してください")
        if not self.config.overview_channels:
            raise HandsError("研究全体のチャンネルが決まっていません（agents.csv の overview の行）")
        name = self.config.overview_channels[0]
        channel = (await self.assistant.channel_ids()).get(name)
        if not channel:
            raise HandsError(f"研究全体のチャンネル（{name}）に Kei Agent が入っていません")
        slack = self.assistant.slack
        # 長すぎる本文は、頭の1つ目だけをチャンネルに、残りをスレッドに出す
        head, *rest = split_text(text)
        try:
            posted = await slack.chat_postMessage(channel=channel, markdown_text=head, unfurl_links=False,
                                                  unfurl_media=False)
            ts = str(posted["ts"])
            # 返信を拾えるように、スレッドを覚えておく
            self.assistant.store.upsert_thread(channel, ts, name, None)
            for chunk in [*rest, *(split_text(details) if details.strip() else ())]:
                await slack.chat_postMessage(channel=channel, thread_ts=ts, markdown_text=chunk, unfurl_links=False,
                                             unfurl_media=False)
        except Exception as e:
            log.warning("手の口から投稿できませんでした", exc_info=True)
            raise HandsError(f"Slack に投稿できませんでした（{type(e).__name__}）") from None
        log.info("手の口から研究全体のチャンネルに投稿しました（%s）", ts)
        return {"channel": name, "ts": ts, "link": await self.assistant.permalink(channel, ts)}

    # 知らせ（Slack につないでいないとき、本体とモジュールが Slack に出すつもりだったもの）

    def _outbox(self):
        from kei_agent.conversation.outbox import Outbox
        return self.assistant.slack if isinstance(self.assistant.slack, Outbox) else None

    def notices(self, done: list[str] | None = None) -> dict:
        """まだ出していない知らせ（古い順）。done に渡した id は「出した」にして、書き換えられない限り次からは返さない
        （頭が Slack に出せたものだけを done で返す。途中で切れても知らせは消えない）。done を渡さなければ、返したものを
        そのまま「出した」にする（done を知らない頭のため）。
        Slack につないでいる間は、Kei Agent が自分で Slack に出しているので空。"""
        outbox = self._outbox()
        if outbox is None:
            return {"notices": [], "slack": True}
        outbox.mark_delivered([str(i) for i in done or []])
        found = outbox.pending()
        if done is None:
            outbox.mark_delivered([item["id"] for item in found])
        return {"notices": [{k: item[k] for k in ("id", "channel", "thread_ts", "thread", "text")}
                            | {"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(item["at"]))}
                            for item in found], "slack": False}

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
    # 確認の合図は最後の行だけを見る（本文の途中に引用された合図では止めない）
    last = next((line.strip() for line in reversed((result.text or "").splitlines())
                 if line.strip() and not line.strip().startswith("<<kei-agent-final")), "")
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
        # 決まった形でなくても、頭には中身をそのまま見せる（Slack に出すわけではない）
        return (result.text or "").strip()
