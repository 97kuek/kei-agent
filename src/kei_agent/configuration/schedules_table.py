"""定期処理の表（利用者のフォルダの schedules.csv）。決まった時刻の処理の時刻とオンオフを、1か所で変える。

1行に1つの処理。列は name, enabled, time。

- name … 本体の処理（daily・review・night・maintenance）か、モジュールの module.toml の [schedules] の名前
- enabled … true / false（大文字でもよい）。false なら、その処理を行わない
- time … HH:MM（ローカル時刻）。enabled が true なら要る
- 表に無い処理は、既定の時刻で動く（本体の既定と module.toml の default）

config.toml には時刻（[schedule] の daily などと、[maintenance] の time・enabled）を書かない（書いてあれば、
表に書くよう知らせて止める）。App Home では変えない。読んだ中身は、config.toml と同じ形（[schedule] の時刻、空文字は
行わない）にして load_config に渡す。
"""

from __future__ import annotations

from pathlib import Path

from kei_agent.configuration.csv_table import TableError, parse_bool, read_text, rows
from kei_agent.framework import modules

SCHEDULES_FILE = "schedules.csv"
COLUMNS = ("name", "enabled", "time")
# 本体の処理と既定の時刻（保守は config.toml の [maintenance] の残りの項目と組む）
CORE_TIMES = {"night": "00:00", "daily": "08:00", "review": "21:00", "maintenance": "22:00"}
def known_names() -> dict[str, str]:
    """書ける処理の名前と既定の時刻（本体のものと、知っているすべてのモジュールのもの。オフのモジュールのものも書ける）。"""
    found = dict(CORE_TIMES)
    for spec in modules.known().values():
        found.update({s.name: s.default for s in spec.schedules})
    return found


def parse(text: str, name: str = SCHEDULES_FILE) -> dict[str, str]:
    """表を、処理の名前 → 時刻（行わないなら空文字）にする。"""
    known = known_names()
    found: dict[str, str] = {}
    for line, row in rows(text, name, COLUMNS):
        schedule = row["name"]
        where = f"{name} の {line} 行目（{schedule}）"
        if schedule not in known:
            raise TableError(f"{where}: 知らない処理です（書けるもの: {', '.join(sorted(known))}）")
        if schedule in found:
            raise TableError(f"{where}: {schedule} の行が2つあります")
        on = parse_bool(row["enabled"], where)
        time = row["time"]
        if time and not modules.HHMM.match(time):
            raise TableError(f"{where} の time は HH:MM（例 07:00）で書いてください: {time!r}")
        if on and not time:
            raise TableError(f"{where}: 行うなら time を書いてください")
        found[schedule] = time if on else ""
    return found


def load(path: Path) -> dict[str, str]:
    return parse(read_text(path), path.name)
