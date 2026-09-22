"""Slack（ボタンと App Home）から変える設定: テーマごとの接続先と、決まった時刻の処理の時刻。

柵そのもの（書き込み先、読ませない場所、基本の接続先）は `config.toml` に残し、ここでは扱わない。
保存先は Kei Agent の SQLite（表は store.py の SCHEMA）。テーマのディレクトリは Claude が書けるので、そこに置くと Claude が自分で
許可を足せてしまう（docs/design.md の9章）。
"""

from __future__ import annotations

import re
import sqlite3
import time

from kei_agent.config import HHMM, Config
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
    "maintenance": "毎晩の保守とバックアップ",
}

_REQUEST = re.compile(rf"^{re.escape(CONNECT_MARKER)}\s*(\S+?)\s*(?:[（(](.*?)[）)])?\s*$")


# テーマごとの接続先

def theme_domains(store: Store, theme: str) -> list[str]:
    rows = store.conn.execute(
        "SELECT domain FROM theme_domains WHERE theme = ? ORDER BY domain", (theme,)).fetchall()
    return [r["domain"] for r in rows]


def all_theme_domains(store: Store) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for r in store.conn.execute("SELECT theme, domain FROM theme_domains ORDER BY theme, domain"):
        result.setdefault(r["theme"], []).append(r["domain"])
    return result


def allow_domain(store: Store, theme: str, domain: str, reason: str) -> None:
    with store.conn:
        store.conn.execute(
            "INSERT OR IGNORE INTO theme_domains (theme, domain, reason, added_at) VALUES (?, ?, ?, ?)",
            (theme, domain.strip().lower(), reason, time.time()),
        )


def remove_domain(store: Store, theme: str, domain: str) -> None:
    with store.conn:
        store.conn.execute("DELETE FROM theme_domains WHERE theme = ? AND domain = ?", (theme, domain))


def drop_theme(store: Store, theme: str) -> None:
    """テーマを閉じたら、そのテーマで許可した接続先も消す。"""
    with store.conn:
        store.conn.execute("DELETE FROM theme_domains WHERE theme = ?", (theme,))


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
    with store.conn:
        cur = store.conn.execute(
            """INSERT INTO domain_requests (channel, thread_ts, theme, domain, reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (channel, thread_ts, theme, domain, reason, time.time()),
        )
    return int(cur.lastrowid)


def get_request(store: Store, request_id: int) -> sqlite3.Row | None:
    return store.conn.execute("SELECT * FROM domain_requests WHERE id = ?", (request_id,)).fetchone()


def resolve_request(store: Store, request_id: int, status: str) -> bool:
    """まだ決まっていない申し出を allowed / denied にする。もう決まっていたら False（ボタンの2度押し）。"""
    with store.conn:
        cur = store.conn.execute(
            "UPDATE domain_requests SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
            (status, time.time(), request_id),
        )
    return cur.rowcount == 1


def pending_requests(store: Store, channel: str, thread_ts: str) -> int:
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM domain_requests WHERE channel = ? AND thread_ts = ? AND status = 'pending'",
        (channel, thread_ts)).fetchone()
    return int(row["n"])


def take_decisions(store: Store, channel: str, thread_ts: str) -> list[sqlite3.Row]:
    """決まったが、まだ Claude に伝えていない申し出。取り出したら伝えたことにする。"""
    with store.conn:
        rows = store.conn.execute(
            """SELECT * FROM domain_requests WHERE channel = ? AND thread_ts = ?
               AND status != 'pending' AND resumed = 0 ORDER BY id""", (channel, thread_ts)).fetchall()
        store.conn.execute(
            "UPDATE domain_requests SET resumed = 1 WHERE channel = ? AND thread_ts = ? AND status != 'pending'",
            (channel, thread_ts))
    return rows


# 決まった時刻の処理

def _get(store: Store, key: str) -> str | None:
    row = store.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(store: Store, key: str, value: str) -> None:
    with store.conn:
        store.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


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


# 声で知らせるか（docs/voice.md の7節）

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
    聞きたいときだけ Slack から入れる（docs/voice.md の7節）。
    """
    return _get(store, LISTEN_KEY) == "1"


def set_listening(store: Store, enabled: bool) -> None:
    _set(store, LISTEN_KEY, "1" if enabled else "0")
