"""Slack（ボタンと App Home）から変える設定: テーマごとの接続先と、決まった時刻の処理の時刻。

柵そのもの（書き込み先、読ませない場所、基本の接続先）は `config.toml` に残し、ここでは扱わない。
保存先は Kei Agent の SQLite（表は store.py の SCHEMA）。テーマのディレクトリは Claude が書けるので、そこに置くと Claude が自分で
許可を足せてしまう（docs/architecture.md）。
"""

from __future__ import annotations

import re
import sqlite3

from kei_agent.config import HHMM, MODEL_ACTORS, AgentProfile, Config
from kei_agent.guard import valid_domain
from kei_agent.store import Store

# Claude がつながらなかったときに、返答の最後に書く行（prompts/system.md）
CONNECT_MARKER = "🔒 接続:"

SCHEDULE_NAMES = ("literature", "daily", "review", "night", "maintenance")
SCHEDULE_LABELS = {
    "literature": "先行研究の新着",
    "daily": "Daily",
    "review": "Retro & Planning",
    "night": "🌙 をつけた Task",
    "maintenance": "保守とバックアップ",
}

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
    if name not in SCHEDULE_NAMES:
        raise ValueError(f"知らない処理です: {name}")
    if not HHMM.match(hhmm):
        raise ValueError(f"時刻は HH:MM で指定してください: {hhmm}")
    _set(store, f"schedule.{name}.time", hhmm)
    _set(store, f"schedule.{name}.enabled", "1" if enabled else "0")


# 声で知らせるか（docs/architecture.md の「声のレイヤ」）

VOICE_KEY = "voice.enabled"
LISTEN_KEY = "voice.listening"


def voice_enabled(store: Store) -> bool:
    """声で知らせるか。既定は切（机にロボットが無い状態で急に喋り出さない）。

    「Mac で鳴らすか Stack-chan で鳴らすか」は声のレイヤが決める（`/status` で分かる）。
    本体が持つのは「知らせを送るかどうか」だけ。
    """
    return _get(store, VOICE_KEY) == "1"


def set_voice(store: Store, enabled: bool) -> None:
    _set(store, VOICE_KEY, "1" if enabled else "0")


def listening_enabled(store: Store) -> bool:
    """マイクで聞くか。**既定は切**。

    常に録っているのは落ち着かないし、講義中に「経過」「計測」のような同音で反応しても困る。
    聞きたいときだけ Slack から入れる（docs/architecture.md の「声のレイヤ」）。
    """
    return _get(store, LISTEN_KEY) == "1"


def set_listening(store: Store, enabled: bool) -> None:
    _set(store, LISTEN_KEY, "1" if enabled else "0")


# actor ごとの provider。App Home の値は config.toml を書き換えず、次の実行からだけ上書きする。
_PROFILE_PROVIDERS = frozenset({"claude", "codex"})


def set_agent_provider(store: Store, agent: str, provider: str) -> None:
    """実行器を切り替える。model / effort は保存しない。"""
    if agent not in MODEL_ACTORS:
        raise ValueError(f"未知のagentです: {agent}")
    if provider not in _PROFILE_PROVIDERS:
        raise ValueError("provider は claude または codex にしてください")
    _set(store, f"agent.{agent}.provider", provider)


def clear_agent_profile(store: Store, agent: str) -> None:
    """App Home の provider 選択を外し、未選択へ戻す。"""
    if agent not in MODEL_ACTORS:
        raise ValueError(f"未知のagentです: {agent}")
    store.delete_setting(f"agent.{agent}.provider")


def has_agent_profile_override(store: Store, agent: str) -> bool:
    """App Home で provider を明示選択したか。"""
    if agent not in MODEL_ACTORS:
        raise ValueError(f"未知のagentです: {agent}")
    return _get(store, f"agent.{agent}.provider") is not None


def selected_provider(config: Config, store: Store, actor: str) -> str:
    """actor が明示選択した provider。空なら実行しない。"""
    if actor not in MODEL_ACTORS:
        raise ValueError(f"未知のagentです: {actor}")
    return _get(store, f"agent.{actor}.provider") or config.agent_profiles[actor].provider


def agent_profile(config: Config, store: Store, agent: str) -> AgentProfile:
    if agent not in MODEL_ACTORS:
        raise ValueError(f"未知のagentです: {agent}")
    return AgentProfile(provider=selected_provider(config, store, agent))
