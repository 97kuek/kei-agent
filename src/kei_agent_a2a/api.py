"""モジュールの担当プロセスがコアとやり取りする窓口（枠の版 1。docs/extensibility.md の「コアとモジュール」）。

担当プロセスのコード（modules/<名前>/agent.py と、そこから読む同じフォルダのファイル）が読み込んでよい
Kei Agent の部品は、この kei_agent_a2a.api だけ。本体側（module.py）の窓口は kei_agent.api。

agent.py には次を置く。起動は共通のコマンド（`kei-agent-module <名前>`）が、module.toml の [process] の番地で行う。

- `SKILLS` … 名刺に載せる仕事の一覧（AgentSkill）。本体の振り分け係が読む。自由な質問は id を ASK にする
- `async background(executor)`（任意）… 担当と同じプロセスで動かし続ける仕事（声ならマイクの会話）。起動のときに
  始まり、止めるときに止まる
- `class Executor(SkillExecutor)` … `async handle(updater, metadata, text)` で仕事をこなす。
  `metadata["skill"]` が仕事の id、`text` が本文（本体の core.ask_agent が渡した材料の JSON）、
  `metadata["provider"]` が App Home で選んだ provider。終わったら `await self.done(updater, 一言, data)`、
  断るなら `await self.fail(updater, 理由)`。自由な質問（ASK）は `await self.answer(updater, text)` に渡すと、
  会話の続きも含めて、ほかの担当と同じ形で答える。`self.config` と `self.store` は土台が用意し、
  `self.records` はこのモジュールだけの記録（本体側の core.records と同じもの）
- `DESCRIPTION`（任意）… 名刺の説明。無ければ module.toml の description

手で動かすコマンド（setup など）は commands.py に `COMMANDS = {"名前": main(argv)}` を置く（`kei-agent-module
<名前> <コマンド>`）。Notion はゲートウェイ経由（gateway_notion の名前で届くホームが決まる）、Toggl は load_toggl。
モジュールの設定（module.toml の [settings] と、config.toml の [<名前>]）は `settings(config, 名前)` で読む。
本体の問い合わせ口に研究・大学・仕事の中身を聞くのは `ask_orchestrator`、Slack の外から依頼を置くのは `put_request`。

研究テーマを受け持つモジュール（[channels] に "*"）の担当は、`SkillExecutor.workspace(ask)` を `channel_workspace` で
書き換えてテーマの作業場で動かす。ジョブの待ち行列（pueue）は `Pueue` と、仕事の名前 SUBMIT_JOB など（本体との約束）。

A2A ではない常駐のプロセス（module.toml の [process] に kind = "service"）は、agent.py の代わりに service.py に
`serve(config, port) -> int` を置く（Notion のモジュールのゲートウェイ）。起動は同じく `kei-agent-module <名前>`。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import AgentSkill

from kei_agent import a2a, agents, ask, modules, runner, themes, version
from kei_agent.agent_policy import policy_of
from kei_agent.config import MAIN_CLIENT, Config, NotionConfig, load_config, notion_id
from kei_agent.dates import WEEKDAYS, day_label, parse_time, weekday
from kei_agent.jobs import CANCEL_JOB, FORGET_JOB, LIST_JOBS, SUBMIT_JOB, Pueue
from kei_agent.model_json import json_list, json_object
from kei_agent.model_policy import ModelPolicyError, resolve, resolve_selected
from kei_agent.notion import (
    GATEWAY_TOKEN_ENV,
    Notion,
    NotionError,
    Setup,
    append_blocks,
    gateway_client_token,
    gateway_notion,
    safe_to_resend,
)
from kei_agent.notion_store import markdown_to_blocks, plain_text, rich_text
from kei_agent.records import Records
from kei_agent.themes import Workspace
from kei_agent.timelog import Toggl, TogglError, load_toggl
from kei_agent_a2a.executor import ASK, SkillExecutor, asked_days
from kei_agent_a2a.run import progress

API_VERSION = modules.API_VERSION
__all__ = ["API_VERSION", "ASK", "CANCEL_JOB", "FORGET_JOB", "GATEWAY_TOKEN_ENV", "LIST_JOBS", "MAIN_CLIENT",
           "RUNNING_VERSION", "SUBMIT_JOB", "WEEKDAYS", "AIError", "AgentSkill", "Config", "Notion", "NotionConfig",
           "NotionError", "OrchestratorError", "Pueue", "Records", "Setup", "SkillExecutor", "TaskUpdater", "Toggl",
           "TogglError", "Workspace", "ai_runs_shell", "append_blocks", "ask_orchestrator", "asked_days",
           "channel_workspace", "day_label", "gateway_client_token", "gateway_notion", "json_list", "json_object",
           "load_config", "load_toggl", "markdown_to_blocks", "notion_id", "parse_time", "plain_text", "progress",
           "put_request", "requested_days", "rich_text", "run_ai", "safe_to_resend", "settings", "theme_folders",
           "weekday", "workspace"]
# このプロセスが起動したときの版（commit）。常駐のプロセス（[process] kind = "service"）は /health で返す
RUNNING_VERSION = version.RUNNING
# 本文の JSON の days で受け付ける上限（日）
MAX_DAYS = 400


class AIError(RuntimeError):
    """AI を動かせなかった。limit_reset_at があれば、上限に当たった（本体が明けてからやり直す）。"""

    def __init__(self, reason: str, limit_reset_at: float | None = None):
        super().__init__(reason)
        self.limit_reset_at = limit_reset_at


def ai_runs_shell(actor: str) -> bool:
    """その名前の AI の実行役が、シェル（コマンド）を使えるか。実行役がいなければ False。"""
    try:
        return policy_of(actor).shell
    except ValueError:
        return False


class OrchestratorError(RuntimeError):
    """本体の問い合わせ口に聞けなかった（住所が無い、つながらない、断られた）。理由は本文。"""


async def ask_orchestrator(config: Config, actor: str, question: str, theme: str = "") -> str:
    """本体の問い合わせ口（config.toml の [a2a] orchestrator）に、研究・大学・仕事の中身を聞く。

    担当を呼べるのは本体だけ。本体が選択済みの provider で、読むだけで担当に聞き、Slack に出すときと同じ出力の
    確認を通した答えを返す。actor は research / course / work（研究なら theme も）。聞けなければ OrchestratorError。
    """
    url = config.a2a.orchestrator
    if not url:
        raise OrchestratorError("本体の住所が config.toml の [a2a] orchestrator にありません")
    # 本体は担当の AI を動かすので、待つ時間はその上限時間を足しておく
    timeout = config.run_timeout_minutes * 60 + config.a2a.timeout_seconds
    reply = await agents.ask(a2a.Agent(url, config.a2a_token, timeout=timeout), agents.ASK,
                             text=json.dumps({"actor": actor, "question": question, "theme": theme},
                                             ensure_ascii=False))
    if not reply.ok:
        raise OrchestratorError(reply.text or "返事が空でした")
    return reply.text


def put_request(config: Config, theme: str, text: str, *, note: bool = False) -> Path:
    """Slack の外から依頼を置く。本体が数秒で拾い、テーマのチャンネルにスレッドを立てて、いつもどおり作業する。

    theme は Slack のチャンネル名。note なら作業させず、決まったこととして記録だけする。置いたファイルの場所を返す。
    """
    return ask.write_ask(config, theme, text, "note" if note else "request")


def requested_days(text: str, default: int, maximum: int = MAX_DAYS) -> int:
    """本文の JSON の days（何日先まで／何日ぶん）。無いか、数字でないか、範囲の外なら既定のまま。

    本体（module.py）の core.ask_agent は、材料を本文の JSON で渡す（metadata には provider だけ）。
    """
    try:
        days = int((json.loads(text) or {}).get("days", default))
    except (TypeError, ValueError, AttributeError):
        return default
    return days if 1 <= days <= maximum else default


def settings(config: Config, module: str) -> dict:
    """そのモジュールの設定（module.toml の [settings] の既定に、config.toml の [<名前>] を重ねた写し）。"""
    return config.settings(module)


def theme_folders(config: Config) -> dict[str, Path]:
    """研究テーマの名前とフォルダ（既定の置き場所の下と、themes.toml の既存のフォルダ）。"""
    return themes.all_themes(config)


def channel_workspace(config: Config, channel_name: str, allowed_domains=(), *, create: bool = True) -> Workspace:
    """研究テーマ（と研究全体）のチャンネルの作業場。core.work の ask に添えて届く channel_name と allowed_domains から作る。

    作業場の無いチャンネル（モジュールのチャンネルなど）なら ValueError。create なら、無ければ作る（読むだけの回は作らない）。
    """
    ws = themes.resolve(config, channel_name)
    if ws.cwd is None or ws.kind not in (themes.ChannelKind.THEME, themes.ChannelKind.OVERVIEW):
        raise ValueError(f"#{ws.channel_name} には作業用ディレクトリがありません")
    ws = replace(ws, allowed_domains=tuple(allowed_domains or ()))
    if create:
        themes.ensure_workspace(ws)
    return ws


def workspace(config: Config, agent: str) -> Path:
    """その担当の作業場（状態の置き場の下）。覚えておきたいもの（一度見た記事など）を置いてよい。"""
    ws = themes.agent_workspace(config, agent)
    assert ws.cwd is not None
    return ws.cwd


async def run_ai(config: Config, store, agent: str, use_case: str, prompt: str, *, provider: str = "",
                 prompt_file: str = "", profile: bool = True) -> str:
    """その担当の用途（module.toml の [use_cases]）で AI を1回動かし、答えの本文を返す。

    作業場は読むだけ。どこまで触れるかは制限の表（module.toml の [actor] と、用途の offline）が決める。
    provider を渡さなければ App Home の選択を使う。prompt_file を渡すと、指示書をそのファイル（モジュールの
    フォルダの中。利用者のフォルダの prompts/ に同じ名前があれば、そちら）に差し替える。profile を False に
    すると、依頼者のプロフィールを差し込まない（JSON だけを返す係など）。動かせなければ AIError。
    """
    try:
        recipe = (resolve(agent, provider, use_case) if provider
                  else resolve_selected(config, store, agent, use_case))
    except ModelPolicyError as e:
        raise AIError(str(e)) from None
    ws = themes.agent_workspace(config, agent)
    ws = replace(ws, system_prompt=config.prompt_file(prompt_file, module=agent) if prompt_file else None,
                 profile=profile)
    result = await runner.run_model(config, runner.ExecutionRequest(ws, recipe, None, "", "", read_only=True), prompt)
    if result.is_error:
        raise AIError(result.failure_reason(), result.limit_reset_at)
    return result.text
