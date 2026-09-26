"""エージェントが選択済み provider を1回動かす（研究・大学・仕事で共通）。docs/architecture.md の「振り分けと A2A」

どのエージェントも、同じやり方で provider を動かす。

- 起動は `kei_agent.runner.run_model` だけ。どこまで触れるかは制限の表（`kei_agent.agent_policy`）が決める
- 依頼は JSON（`prompt`、`session_id`、`channel`、`thread_ts`、`read_only`、`use_case`、`provider`）
- 用途は依頼に書いてあればそれ、無ければ担当の軽い分類器で決める
- 途中の経過は固定の利用者向け状態だけをタスクの状態に流す。道具名や返答断片は流さない
- 返事は共通の封筒（`envelope.py`）。`data` には `RunResult` の受け取る項目だけを入れる
- 上限（レートリミット）に当たったら `limit_reset_at` を載せて返す。やり直しの約束は本体が持つ
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState

from kei_agent import runner
from kei_agent.agents import FIELDS as RESULT_FIELDS
from kei_agent.config import Config
from kei_agent.model_classifier import classify
from kei_agent.model_policy import ResolvedModel, UseCase, resolve, resolve_selected
from kei_agent.research import is_manual_use_case
from kei_agent.themes import Workspace
from kei_agent_a2a import envelope

log = logging.getLogger(__name__)

# 経過1つの長さの上限（タスクの記録が長くなりすぎないように）
PROGRESS_LIMIT = 800
NO_PROMPT = "依頼の JSON に prompt が要ります"


def ask_json(text: str) -> dict:
    """届いた依頼（JSON）を辞書にする。prompt が無ければ断る。"""
    try:
        ask = json.loads(text)
    except ValueError:
        raise ValueError(NO_PROMPT) from None
    if not isinstance(ask, dict) or not str(ask.get("prompt") or "").strip():
        raise ValueError(NO_PROMPT)
    return ask


async def recipe_for(config: Config, store, agent: str, ask: dict) -> ResolvedModel:
    """その回の recipe。provider は依頼の指定（無ければ App Home の選択）、用途は依頼か分類から。

    分類器が利用上限に当たったら UsageLimited、provider や用途が決まらなければ ModelPolicyError / ValueError。
    """
    provider = str(ask.get("provider") or "")
    if ask.get("use_case"):
        use_case = UseCase(str(ask["use_case"]))
    else:
        use_case = await classify(config, store, agent, str(ask["prompt"]), provider=provider or None)
    manual = is_manual_use_case(use_case)
    return (resolve(agent, provider, use_case, manual=manual) if provider
            else resolve_selected(config, store, agent, use_case, manual=manual))


async def progress(updater: TaskUpdater, payload: dict) -> None:
    """途中の様子を、タスクの状態に流す。"""
    short = {k: str(v)[:PROGRESS_LIMIT] for k, v in payload.items()}
    await updater.update_status(
        TaskState.TASK_STATE_WORKING,
        message=updater.new_agent_message([Part(text=json.dumps(short, ensure_ascii=False))]))


async def execute(config: Config, ws: Workspace, ask: dict, updater: TaskUpdater,
                  recipe: ResolvedModel) -> str:
    """用途別 recipe を確定済みの agent 実行を、封筒にして返す。"""
    assert ws.cwd is not None

    async def on_activity(activity: str) -> None:
        await progress(updater, {"activity": activity})

    log.info("%s を動かします: %s（%s）", recipe.provider, ws.channel_name, ws.cwd)
    result = await runner.run_model(
        config,
        runner.ExecutionRequest(
            ws, recipe, ask.get("session_id"), ask.get("channel", ""), ask.get("thread_ts", ""),
            read_only=bool(ask.get("read_only")),
        ),
        ask["prompt"], on_activity=on_activity,
    )
    log.info("%s が終わりました: %s（エラー: %s）", recipe.provider, ws.channel_name, result.is_error)
    # 受け取る側が読む項目だけを渡す（検証前の途中の文など、内部の項目は外に出さない）
    data = {key: value for key, value in asdict(result).items() if key in RESULT_FIELDS}
    return envelope.reply(
        text=result.text,
        data=data,
        ok=not result.is_error,
        limit_reset_at=result.limit_reset_at,
        cost_usd=result.cost_usd,
    )


def _json_values(text: str, opener: str) -> list[object]:
    """文の中にある、`opener`（`[` か `{`）で始まる JSON を、外側のものだけ前から順に。

    各位置から1度だけ読み、読めた値の中は飛ばす（全部の括弧の組を試さない）。
    """
    decoder = json.JSONDecoder()
    found: list[object] = []
    position = text.find(opener)
    while position >= 0:
        try:
            value, end = decoder.raw_decode(text, position)
        except ValueError:
            position = text.find(opener, position + 1)
            continue
        found.append(value)
        position = text.find(opener, end)
    return found


def json_reply(text: str) -> list[dict]:
    """AI に JSON で答えさせたときの、返事の読み取り（前後に文やコードの囲みが付いていても拾う）。

    「接続が拒否されました」のような文を「予定0件」と取り違えないよう、JSON の配列が
    見つからなければ断る（ValueError）。前置きに角括弧があっても、読めるものを探す。

    後ろに出典（`[1, 2]`）のような別の配列が付くことがあるので、**中身のある配列を優先**する。
    中身のある配列が複数あるときは、後ろのもの（答えの本体は最後に来る）。
    """
    text = text or ""
    lists = [value for value in _json_values(text, "[") if isinstance(value, list)]
    for found in reversed(lists):
        items = [item for item in found if isinstance(item, dict)]
        if items:
            return items
    if lists:
        return []
    raise ValueError(f"返事を読めません（JSON の配列がありません）: {text[:200]}")


def json_object(text: str, key: str) -> dict:
    """返事から、`key` を持つ JSON オブジェクトを拾う（コードの囲みや前置きが付いていても読む）。"""
    for value in _json_values(text or "", "{"):
        if isinstance(value, dict) and key in value:
            return value
    raise ValueError("JSON オブジェクトがありません")


async def finish(updater: TaskUpdater, payload: str) -> None:
    """封筒を見て、A2A のタスクを終わらせる。`ok: false` なら failed にする。

    うまくいかなかったのに completed で返すと、頼んだ側は失敗に気づけない。
    """
    try:
        ok = bool(json.loads(payload).get("ok"))
    except (ValueError, AttributeError):
        ok = False
    message = updater.new_agent_message([Part(text=payload)])
    await (updater.complete(message) if ok else updater.failed(message))
