"""App Home（Slack で Kei Agent を開いたときのタブ）に出す設定画面。

見出しと操作だけの1画面にする（説明文は置かない）。置くのは、動いているもの、担当ごとの AI とアカウント
（agents.csv の値を見せるだけ。変えるのは agents.csv）、モジュールの項目（class Module の home。声の「知らせる」
「聞く」など）だけ。`config.toml` の柵は出さない。
"""

from __future__ import annotations

import time
from datetime import datetime

from kei_agent.configuration.config import AgentProfile, Config
from kei_agent.conversation.slack_text import format_duration
from kei_agent.framework import modules
from kei_agent.storage import settings
from kei_agent.storage.store import Store

REFRESH_ACTION = "kei_agent_home_refresh"
MODULE_ACTION = "kei_agent_home_module"          # :<モジュール>:<名前>
# 本体の実行役の表示名。モジュールの実行役は module.toml の label（agent_labels）
CORE_AGENT_LABELS: dict[str, str] = {}
CROSS_AGENT_LABELS = {"router": "振り分け"}
# 決まった時刻の処理は、スレッドを持たない実行として記録される
TRIGGER_LABELS = {"message": "依頼", "job": "ジョブの結果", "voice": "声からの依頼",
                  "night": "夜間の Task", "handoff": "引き継ぎ", "daily": "Daily", "review": "振り返り"}


def agent_labels(config: Config) -> dict[str, str]:
    """AI を使う実行役と表示名（本体の担当、使うモジュール、横断の係の順）。"""
    labels = dict(CORE_AGENT_LABELS)
    labels.update({spec.name: spec.label for spec in modules.enabled(config.modules) if spec.actor})
    labels.update(CROSS_AGENT_LABELS)
    return labels


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text}, "value": value}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    button = {"type": "button", "action_id": action_id, "value": value,
              "text": {"type": "plain_text", "text": text}}
    if style:
        button["style"] = style
    return button


def module_action_id(module: str, name: str) -> str:
    """モジュールの項目の action_id（押されると、そのモジュールの on_home_action(name, action)）。"""
    return f"{MODULE_ACTION}:{module}:{name}"


def checkboxes(action_id: str, options: dict[str, str], chosen: set[str]) -> dict:
    """値 → 表示名のチェック。Slack は空の initial_options を受け付けないので、選んだものが無ければ付けない。"""
    element = {"type": "checkboxes", "action_id": action_id,
               "options": [_option(text, value) for value, text in options.items()]}
    initial = [_option(text, value) for value, text in options.items() if value in chosen]
    if initial:
        element["initial_options"] = initial
    return element


def _now_working(config: Config, store: Store, now: float | None = None) -> list[str]:
    """いま動いている依頼、走っているジョブ、上限が明けるのを待っている依頼、返事待ちのスレッドを、短い行にして返す。"""
    now = time.time() if now is None else now
    lines = []
    for run in store.open_runs():
        kind = TRIGGER_LABELS.get(run["trigger"]) or settings.schedule_label(config, run["trigger"])
        lines.append(f"⏳ *#{run['channel_name']}* {kind}（{format_duration(now - run['started_at'])}）")
    for job in store.active_jobs():
        started = "実行中" if job.status == "running" else "順番待ち"
        lines.append(f"🧪 ジョブ {job.id}「{job.name}」{started}"
                     f"（投入から {format_duration(now - job.submitted_at)}）")
    for _, payload in store.pending_deferred("request"):
        until = store.limit_until(str(payload.get("provider") or ""))
        when = f"{datetime.fromtimestamp(until):%H:%M} ごろから" if until > now else "まもなく"
        lines.append(f"⏸ *#{payload['channel_name']}* 上限が明けるのを待っている（{when}）")
    for row in store.threads_awaiting():
        lines.append(f"❓ *#{row['channel_name']}* 返事待ち（{format_duration(now - row['awaiting_since'])}）")
    return lines


def _engines(config: Config, agent: str) -> str:
    """その担当の AI と、使うアカウントのフォルダ（agents.csv の engine・engines と claude_account・codex_account）。"""
    profile = config.agent_profiles.get(agent, AgentProfile())
    if not profile.allowed_engines:
        return "未選択"
    accounts = {"claude": profile.claude_account, "codex": profile.codex_account}
    return "・".join(f"{engine.title()}（{accounts.get(engine) or '既定'}）" for engine in profile.allowed_engines)


def build_home(config: Config, store: Store, is_owner: bool,
               module_sections: list[tuple[str, list[dict]]] = ()) -> dict:
    """module_sections は、モジュールの表示名と項目（class Module の home が返した blocks）。定期実行の下に並べる。"""
    if not is_owner:
        return {"type": "home", "blocks": [_mrkdwn("設定を変えられるのは依頼者だけです")]}

    working = _now_working(config, store)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Kei Agent"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*動いているもの*"},
         "accessory": _button("更新", REFRESH_ACTION, "refresh")},
        _mrkdwn("\n".join(working)) if working else _context("なし"),
        {"type": "divider"},
        _mrkdwn("*AI*"),
        _mrkdwn("\n".join(f"{label}  {_engines(config, agent)}" for agent, label in agent_labels(config).items())),
    ]

    for label, items in module_sections:
        blocks += [{"type": "divider"}, _mrkdwn(f"*{label}*"), *items]

    return {"type": "home", "blocks": blocks}
