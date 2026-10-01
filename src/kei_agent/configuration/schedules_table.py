"""定期処理の表（利用者のフォルダの schedules.csv）。決まった時刻の処理の時刻とオンオフを、1か所で変える。

1行に1つの処理。列は name, enabled, time。

- name … 本体の処理（daily・review・night・maintenance）か、モジュールの module.toml の [schedules] の名前
- enabled … true / false（大文字でもよい）。false なら、その処理を行わない
- time … HH:MM（ローカル時刻）。enabled が true なら要る
- 表に無い処理は、既定の時刻で動く（本体の既定と module.toml の default）

config.toml には時刻（[schedule] の daily などと、[maintenance] の time・enabled）を書かない（書いてあれば、
移すよう知らせて止める）。App Home では変えない。読んだ中身は、config.toml と同じ形（[schedule] の時刻、空文字は
行わない）にして load_config に渡す。
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from kei_agent.framework import modules

SCHEDULES_FILE = "schedules.csv"
COLUMNS = ("name", "enabled", "time")
# 本体の処理と既定の時刻（保守は config.toml の [maintenance] の残りの項目と組む）
CORE_TIMES = {"night": "00:00", "daily": "08:00", "review": "21:00", "maintenance": "22:00"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TRUE = {"true": True, "false": False}


class TableError(ValueError):
    pass


def known_names() -> dict[str, str]:
    """書ける処理の名前と既定の時刻（本体のものと、知っているすべてのモジュールのもの。オフのモジュールのものも書ける）。"""
    found = dict(CORE_TIMES)
    for spec in modules.known().values():
        found.update({s.name: s.default for s in spec.schedules})
    return found


def parse(text: str, name: str = SCHEDULES_FILE) -> dict[str, str]:
    """表を、処理の名前 → 時刻（行わないなら空文字）にする。"""
    reader = csv.DictReader(io.StringIO(text))
    header = [column.strip() for column in reader.fieldnames or ()]
    if sorted(header) != sorted(COLUMNS):
        raise TableError(f"{name} の1行目（列の名前）は {','.join(COLUMNS)} にしてください（今は {','.join(header) or '空'}）")
    reader.fieldnames = header
    known = known_names()
    found: dict[str, str] = {}
    for line, raw in enumerate(reader, start=2):
        if None in raw:
            raise TableError(f"{name} の {line} 行目は列が多すぎます")
        row = {key: (value or "").strip() for key, value in raw.items()}
        schedule = row["name"]
        if not schedule or schedule.startswith("#"):
            continue
        where = f"{name} の {line} 行目（{schedule}）"
        if schedule not in known:
            raise TableError(f"{where}: 知らない処理です（書けるもの: {', '.join(sorted(known))}）")
        if schedule in found:
            raise TableError(f"{where}: {schedule} の行が2つあります")
        on = _TRUE.get(row["enabled"].lower())
        if on is None:
            raise TableError(f"{where} の enabled は true か false にしてください: {row['enabled']!r}")
        time = row["time"]
        if time and not _HHMM.match(time):
            raise TableError(f"{where} の time は HH:MM（例 07:00）で書いてください: {time!r}")
        if on and not time:
            raise TableError(f"{where}: 行うなら time を書いてください")
        found[schedule] = time if on else ""
    return found


def read_text(path: Path) -> str:
    try:
        # Excel が付ける BOM は外す
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise TableError(f"{path.name} を UTF-8 として読めません。UTF-8（CSV UTF-8）で保存し直してください") from None


def load(path: Path) -> dict[str, str]:
    return parse(read_text(path), path.name)


def from_config(schedule: dict, maintenance: dict) -> str:
    """config.toml の [schedule] の時刻と [maintenance] の time・enabled から、同じ中身の表を作る（移すとき）。"""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for name, default in known_names().items():
        if name == "maintenance":
            time = str(maintenance.get("time", default))
            on = bool(maintenance.get("enabled", True)) and bool(time)
        else:
            time = str(schedule.get(name, default))
            on = bool(time)
        writer.writerow([name, "true" if on else "false", time or default])
    return out.getvalue()
