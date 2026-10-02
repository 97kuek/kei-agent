"""Slack（App Home）から変える設定: 一時的に切り替えた担当の AI。定期処理の見出し。

柵そのもの（書き込み先、読ませない場所）は `config.toml` に残し、ここでは扱わない。
保存先は Kei Agent の SQLite（表は store.py の SCHEMA）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from kei_agent.configuration.config import HHMM, AgentProfile, Config, model_actors
from kei_agent.framework import modules
from kei_agent.storage.store import Store, schedule_detail

# 本体の定期処理（名前 → 見出し）。モジュールのものは module.toml の [schedules] から足す
CORE_SCHEDULES = {
    "daily": "Daily",
    "review": "Retro & Planning",
    "night": "🌙 をつけた Task",
    "maintenance": "保守とバックアップ",
}


def module_schedules(config: Config) -> list[modules.ScheduleSpec]:
    """使うモジュールの定期処理（設定の modules の順）。"""
    return [s for spec in modules.enabled(config.modules) for s in spec.schedules]


def schedule_names(config: Config) -> tuple[str, ...]:
    """動かす定期処理（モジュールのものを先に。朝の読みものなどは Daily より前に並べる）。

    Daily と振り返りは、受け持つモジュール（core_schedules）があるときだけ。
    """
    core = tuple(name for name in CORE_SCHEDULES
                 if name not in modules.CORE_SCHEDULES or modules.core_schedule_owner(config.modules, name))
    return (*(s.name for s in module_schedules(config)), *core)


def schedule_label(config: Config, name: str) -> str:
    """定期処理の見出し。"""
    if name in CORE_SCHEDULES:
        return CORE_SCHEDULES[name]
    spec = next((s for s in module_schedules(config) if s.name == name), None)
    return spec.label if spec else name


def failed_schedules(config: Config, store: Store, now: datetime) -> list[str]:
    """前回の Daily から今までに、うまくいかなかった定期処理の見出し（朝の一覧の「うまくいかなかったこと」）。

    Notion に残せなかった Daily・振り返りも入れる。
    """
    today = now.date().isoformat()
    last = store.last_schedule("daily", before_day=today)
    since = last["ran_at"] if last else now.timestamp() - 86400
    failed = []
    for row in store.schedule_runs_since(since):
        label = schedule_label(config, row["name"]) if row["name"] in schedule_names(config) else None
        if label is None or (row["name"] == "daily" and row["day"] == today):
            continue    # 定期処理でないもの、いま作っている Daily
        detail = schedule_detail(row)
        if detail.get("status") == "error":
            failed.append(label)
        elif row["name"] in ("daily", "review") and detail.get("status") == "posted" and not detail.get("notion_url"):
            failed.append(f"{label}（Notion に残せず）")
    return failed


# 決まった時刻の処理

def _get(store: Store, key: str) -> str | None:
    return store.setting(key)


def _set(store: Store, key: str, value: str) -> None:
    store.set_setting(key, value)


def _config_time(config: Config, name: str) -> str:
    if name == "maintenance":
        return config.maintenance.time
    if name in config.schedule.module_times:
        return config.schedule.module_times[name]
    return getattr(config.schedule, name)


def schedule_time(config: Config, name: str) -> str:
    """いま使う時刻（定期処理の表 schedules.csv。無ければ既定）。止めているときは空文字（その処理を行わない）。"""
    if name == "maintenance":
        hhmm = config.maintenance.time if config.maintenance.enabled else ""
    else:
        hhmm = _config_time(config, name)
    return hhmm if HHMM.match(hhmm or "") else ""


# actor ごとの provider。正は担当の表（agents.csv）の engine。App Home の値は表を書き換えず、
# 本体を起動し直すまでの一時的な上書き
_PROFILE_PROVIDERS = frozenset({"claude", "codex"})


def set_agent_provider(store: Store, agent: str, provider: str) -> None:
    """実行器を切り替える。model / effort は保存しない。"""
    if agent not in model_actors():
        raise ValueError(f"未知のagentです: {agent}")
    if provider not in _PROFILE_PROVIDERS:
        raise ValueError("provider は claude または codex にしてください")
    _set(store, f"agent.{agent}.provider", provider)


def selected_provider(config: Config, store: Store, actor: str) -> str:
    """actor が明示選択した provider。空なら実行しない。"""
    if actor not in model_actors():
        raise ValueError(f"未知のagentです: {actor}")
    return _get(store, f"agent.{actor}.provider") or config.agent_profiles[actor].provider


def table_provider(config: Config, actor: str) -> str:
    """担当の表（agents.csv）に書いた provider（App Home の一時的な切り替えの前のもの）。"""
    return config.agent_profiles[actor].provider


def reset_agent_providers(store: Store) -> list[str]:
    """App Home で一時的に切り替えた provider を消して、担当の表に戻す（本体の起動のとき）。戻した actor を返す。"""
    reset = []
    for actor in sorted(model_actors()):
        key = f"agent.{actor}.provider"
        if _get(store, key) is not None:
            store.delete_setting(key)
            reset.append(actor)
    return reset


def agent_profile(config: Config, store: Store, agent: str) -> AgentProfile:
    if agent not in model_actors():
        raise ValueError(f"未知のagentです: {agent}")
    return replace(config.agent_profiles[agent], provider=selected_provider(config, store, agent))
