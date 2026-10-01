"""Slack（ボタンと App Home）から変える設定: テーマごとの接続先と、決まった時刻の処理の時刻。

柵そのもの（書き込み先、読ませない場所、基本の接続先）は `config.toml` に残し、ここでは扱わない。
保存先は Kei Agent の SQLite（表は store.py の SCHEMA）。テーマのディレクトリは Claude が書けるので、そこに置くと Claude が自分で
許可を足せてしまう（docs/architecture.md）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime

from kei_agent import modules
from kei_agent.configuration.config import HHMM, AgentProfile, Config, model_actors
from kei_agent.guard import valid_domain
from kei_agent.store import Store

# Claude がつながらなかったときに、返答の最後に書く行（prompts/system.md）
CONNECT_MARKER = "🔒 接続:"

# 本体の定期処理（名前 → 見出し、App Home の短い名前）。モジュールのものは module.toml の [schedules] から足す
CORE_SCHEDULES = {
    "daily": ("Daily", "Daily"),
    "review": ("Retro & Planning", "レトプラ"),
    "night": ("🌙 をつけた Task", "夜間"),
    "maintenance": ("保守とバックアップ", "保守"),
}


def module_schedules(config: Config) -> list[modules.ScheduleSpec]:
    """使うモジュールの定期処理（設定の modules の順）。"""
    return [s for spec in modules.enabled(config.modules) for s in spec.schedules]


def schedule_names(config: Config) -> tuple[str, ...]:
    """App Home で扱う定期処理（モジュールのものを先に。朝の読みものなどは Daily より前に並べる）。

    Daily と振り返りは、受け持つモジュール（core_schedules）があるときだけ。
    """
    core = tuple(name for name in CORE_SCHEDULES
                 if name not in modules.CORE_SCHEDULES or modules.core_schedule_owner(config.modules, name))
    return (*(s.name for s in module_schedules(config)), *core)


def schedule_label(config: Config, name: str, short: bool = False) -> str:
    """定期処理の見出し（short なら App Home のチェックに出す短い名前）。"""
    if name in CORE_SCHEDULES:
        return CORE_SCHEDULES[name][1 if short else 0]
    spec = next((s for s in module_schedules(config) if s.name == name), None)
    return (spec.short if short else spec.label) if spec else name


def failed_schedules(config: Config, store: Store, now: datetime) -> list[str]:
    """前回の Daily から今までに、うまくいかなかった定期処理の見出し（朝の一覧の「うまくいかなかったこと」）。

    Notion に残せなかった Daily・振り返りも入れる（何日も残っていなかったのに気づけなかった。2026-09-26）。
    """
    today = now.date().isoformat()
    last = store.last_schedule("daily", before_day=today)
    since = last["ran_at"] if last else now.timestamp() - 86400
    failed = []
    for row in store.schedule_runs_since(since):
        label = schedule_label(config, row["name"]) if row["name"] in schedule_names(config) else None
        if label is None or (row["name"] == "daily" and row["day"] == today):
            continue    # 定期処理でないもの、いま作っている Daily
        detail = json.loads(row["detail"] or "{}") or {}
        if detail.get("status") == "error":
            failed.append(label)
        elif row["name"] in ("daily", "review") and detail.get("status") == "posted" and not detail.get("notion_url"):
            failed.append(f"{label}（Notion に残せず）")
    return failed


def _known_schedule(name: str) -> bool:
    """本体か、知っているどれかのモジュールの定期処理か（Slack のボタンから届いた名前を確かめる）。"""
    return name in CORE_SCHEDULES or any(s.name == name for spec in modules.known().values() for s in spec.schedules)

_REQUEST = re.compile(rf"^{re.escape(CONNECT_MARKER)}\s*(\S+?)\s*(?:[（(](.*?)[）)])?\s*$")


# テーマごとの接続先

def theme_domains(store: Store, theme: str) -> list[str]:
    return [r["domain"] for r in store.theme_domains(theme)]


def all_theme_domains(store: Store) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for r in store.theme_domains():
        result.setdefault(r["theme"], []).append(r["domain"])
    return result


def allow_domain(store: Store, theme: str, domain: str, reason: str) -> None:
    store.add_theme_domain(theme, domain.strip().lower(), reason)


def remove_domain(store: Store, theme: str, domain: str) -> None:
    store.remove_theme_domains(theme, domain)


def drop_theme(store: Store, theme: str) -> None:
    """テーマを閉じたら、そのテーマで許可した接続先も消す。"""
    store.remove_theme_domains(theme)


# Claude からの接続の申し出

def parse_connect_requests(text: str) -> list[tuple[str, str]]:
    """返答の `🔒 接続: <ドメイン>（理由）` を拾う。ぴったりのドメイン名でないものは捨てる。"""
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _REQUEST.match(line.strip())
        if not m:
            continue
        domain = m.group(1).lower()
        if valid_domain(domain) and domain not in [d for d, _ in found]:
            found.append((domain, (m.group(2) or "").strip()))
    return found


def add_request(store: Store, channel: str, thread_ts: str, theme: str, domain: str, reason: str) -> int:
    return store.add_domain_request(channel, thread_ts, theme, domain, reason)


def get_request(store: Store, request_id: int) -> sqlite3.Row | None:
    return store.domain_request(request_id)


def resolve_request(store: Store, request_id: int, status: str) -> bool:
    """まだ決まっていない申し出を allowed / denied にする。もう決まっていたら False（ボタンの2度押し）。"""
    return store.resolve_domain_request(request_id, status)


def pending_requests(store: Store, channel: str, thread_ts: str) -> int:
    return store.count_pending_domain_requests(channel, thread_ts)


def take_decisions(store: Store, channel: str, thread_ts: str) -> list[sqlite3.Row]:
    """決まったが、まだ Claude に伝えていない申し出。取り出したら伝えたことにする。"""
    return store.take_domain_decisions(channel, thread_ts)


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


def schedule_setting(config: Config, store: Store, name: str) -> tuple[str, bool]:
    """(時刻, 動かすか)。Slack で変えていなければ config.toml の値。"""
    base = _config_time(config, name)
    hhmm = _get(store, f"schedule.{name}.time")
    enabled = _get(store, f"schedule.{name}.enabled")
    return (hhmm if hhmm is not None else base,
            enabled == "1" if enabled is not None else bool(base))


def schedule_time(config: Config, store: Store, name: str) -> str:
    """いま使う時刻。止めているときは空文字（その処理を行わない）。"""
    if name == "maintenance" and not config.maintenance.enabled:
        return ""
    hhmm, enabled = schedule_setting(config, store, name)
    return hhmm if enabled and HHMM.match(hhmm or "") else ""


def set_schedule(store: Store, name: str, hhmm: str, enabled: bool) -> None:
    if not _known_schedule(name):
        raise ValueError(f"知らない処理です: {name}")
    if not HHMM.match(hhmm):
        raise ValueError(f"時刻は HH:MM で指定してください: {hhmm}")
    _set(store, f"schedule.{name}.time", hhmm)
    _set(store, f"schedule.{name}.enabled", "1" if enabled else "0")


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
