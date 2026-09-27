"""モジュールの担当プロセスがコアとやり取りする窓口（枠の版 1。docs/extensibility.md の「コアとモジュール」）。

担当プロセスのコード（modules/<名前>/agent.py と、そこから読む同じフォルダのファイル）が読み込んでよい
Kei Agent の部品は、この kei_agent_a2a.api だけ。本体側（module.py）の窓口は kei_agent.api。

agent.py には次を置く。起動は共通のコマンド（`kei-agent-module <名前>`）が、module.toml の [process] の番地で行う。

- `SKILLS` … 名刺に載せる仕事の一覧（AgentSkill）。本体の振り分け係が読む。自由な質問は id を ASK にする
- `class Executor(SkillExecutor)` … `async handle(updater, metadata, text)` で仕事をこなす。
  `metadata["skill"]` が仕事の id、`text` が本文（本体の core.ask_agent が渡した材料の JSON）、
  `metadata["provider"]` が App Home で選んだ provider。終わったら `await self.done(updater, 一言, data)`、
  断るなら `await self.fail(updater, 理由)`。自由な質問（ASK）は `await self.answer(updater, text)` に渡すと、
  会話の続きも含めて、ほかの担当と同じ形で答える。`self.config` と `self.store` は土台が用意する
- `DESCRIPTION`（任意）… 名刺の説明。無ければ module.toml の description

手で動かすコマンド（setup など）は commands.py に `COMMANDS = {"名前": main(argv)}` を置く（`kei-agent-module
<名前> <コマンド>`）。Notion はゲートウェイ経由（gateway_notion の名前で届くホームが決まる）、Toggl は load_toggl。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import AgentSkill

from kei_agent import modules, runner, themes
from kei_agent.config import Config, load_config
from kei_agent.dates import WEEKDAYS, day_label, parse_time, weekday
from kei_agent.model_json import json_list, json_object
from kei_agent.model_policy import ModelPolicyError, resolve, resolve_selected
from kei_agent.notion import Notion, NotionError, Setup, gateway_notion
from kei_agent.timelog import Toggl, TogglError, load_toggl
from kei_agent_a2a.executor import ASK, SkillExecutor, asked_days
from kei_agent_a2a.run import progress

API_VERSION = modules.API_VERSION
__all__ = ["API_VERSION", "ASK", "WEEKDAYS", "AIError", "AgentSkill", "Config", "Notion", "NotionError", "Setup",
           "SkillExecutor", "TaskUpdater", "Toggl", "TogglError", "asked_days", "day_label", "gateway_notion",
           "json_list", "json_object", "load_config", "load_toggl", "parse_time", "progress", "requested_days",
           "run_ai", "weekday", "workspace"]
# 本文の JSON の days で受け付ける上限（日）
MAX_DAYS = 400


class AIError(RuntimeError):
    """AI を動かせなかった。limit_reset_at があれば、上限に当たった（本体が明けてからやり直す）。"""

    def __init__(self, reason: str, limit_reset_at: float | None = None):
        super().__init__(reason)
        self.limit_reset_at = limit_reset_at


def requested_days(text: str, default: int, maximum: int = MAX_DAYS) -> int:
    """本文の JSON の days（何日先まで／何日ぶん）。無いか、数字でないか、範囲の外なら既定のまま。

    本体（module.py）の core.ask_agent は、材料を本文の JSON で渡す（metadata には provider だけ）。
    """
    try:
        days = int((json.loads(text) or {}).get("days", default))
    except (TypeError, ValueError, AttributeError):
        return default
    return days if 1 <= days <= maximum else default


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
