"""担当の用途で AI を1回だけ動かす。モジュールの窓口の run_ai（本体側の kei_agent.api と、担当側の kei_agent_a2a.api）。

会話にはせず、答えの本文を返す。どこまで触れるかは制限の表（module.toml の [actor] と、用途の offline）が決める。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kei_agent import runner, themes
from kei_agent.config import Config
from kei_agent.model_policy import ModelPolicyError, resolve, resolve_selected
from kei_agent.themes import ChannelKind, Workspace


class AIError(RuntimeError):
    """AI を動かせなかった。limit_reset_at があれば、上限に当たった（本体が明けてからやり直す）。"""

    def __init__(self, reason: str, limit_reset_at: float | None = None):
        super().__init__(reason)
        self.limit_reset_at = limit_reset_at


async def run_once(config: Config, store, agent: str, use_case: str, prompt: str, *, provider: str = "",
                   prompt_file: str = "", profile: bool = True, folder: Path | None = None, channel: str = "",
                   thread_ts: str = "", on_activity=None) -> str:
    """その担当の用途で AI を1回動かし、答えの本文を返す。動かせなければ AIError。

    folder を渡さなければ、担当の作業場を読むだけで動かす。folder を渡すと、そのフォルダの中で動かし、書き込みも
    そのフォルダの中だけ（書けるかどうかは、制限の表の files が決める）。provider を渡さなければ App Home の選択。
    prompt_file は指示書の差し替え（モジュールのフォルダの中）、profile を False にすると依頼者のプロフィールを
    差し込まない（JSON だけを返す係など）。
    """
    try:
        recipe = (resolve(agent, provider, use_case) if provider
                  else resolve_selected(config, store, agent, use_case))
    except ModelPolicyError as e:
        raise AIError(str(e)) from None
    ws = (Workspace(agent, ChannelKind.FOLDER, folder, module=agent) if folder is not None
          else themes.agent_workspace(config, agent))
    ws = replace(ws, system_prompt=config.prompt_file(prompt_file, module=agent) if prompt_file else None,
                 profile=profile)
    request = runner.ExecutionRequest(ws, recipe, None, channel, thread_ts, read_only=folder is None)
    # 経過を見せないときは渡さない（経過の受け口を持たない呼び方と同じ形）
    result = await (runner.run_model(config, request, prompt, on_activity) if on_activity is not None
                    else runner.run_model(config, request, prompt))
    if result.is_error:
        raise AIError(result.failure_reason(), result.limit_reset_at)
    return result.text
