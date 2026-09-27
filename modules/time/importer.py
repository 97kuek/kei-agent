"""Toggl のアプリで直接測った記録を、共通ホームの「時間記録」に取り込む（定期処理 toggl_import）。

Toggl のプロジェクト名の先頭に `研究/`・`大学/`・`仕事/` を付けたものだけを数える。印のないもの（アルバイト、
個人開発）は捨てる。科目やテーマが増えても、このコードは変えなくてよい。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .entries import DOMAINS

# Toggl のプロジェクト名の、領域と名前の区切り
DOMAIN_SEP = "/"
# 何日さかのぼって取り込むか（何晩か止まっても埋まるように）
IMPORT_DAYS = 7
# Slack から送った記録と同じとみなす、開始と長さのずれ（秒）
SAME_ENTRY_SECONDS = 60


def split_project(name: str) -> tuple[str, str] | None:
    """Toggl のプロジェクト名を（領域, 名前）に分ける。印が無ければ None（数えない）。"""
    domain, sep, rest = (name or "").partition(DOMAIN_SEP)
    if not sep or domain.strip() not in DOMAINS.values() or not rest.strip():
        return None
    return domain.strip(), rest.strip()


def import_toggl(toggl, hub, own: list[tuple[float, float]], since: date, until: date) -> dict:
    """Toggl で直接測った記録を、共通ホームの「時間記録」に記録 ID「toggl:<id>」で入れる。

    own は Slack で測った記録の（開始の時刻, 秒数）。開始と長さがどちらも SAME_ENTRY_SECONDS 以内で
    重なる Toggl の記録は、Slack から送った同じものなので飛ばす。入れ済みの ID、計測中、休憩、
    消した記録、印のないプロジェクトも飛ばす。
    """
    # Notion の日付の絞り込みは時差の分ずれることがあるので、1日広く取る
    known = hub.time_ids_since(since - timedelta(days=1))
    counts = {"imported": 0, "own": 0, "known": 0, "unmarked": 0}
    for e in toggl.entries(since, until):
        duration, start = e.get("duration"), e.get("start")
        if (e.get("id") is None or not start or not isinstance(duration, int | float) or duration <= 0
                or e.get("type") == "break" or e.get("deleted_at")):
            continue
        project = str((e.get("project") or {}).get("name") or "")
        found = split_project(project)
        if found is None:
            counts["unmarked"] += 1
            continue
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone()
        if any(abs(started.timestamp() - at) <= SAME_ENTRY_SECONDS
               and abs(duration - seconds) <= SAME_ENTRY_SECONDS for at, seconds in own):
            counts["own"] += 1
            continue
        entry_id = f"toggl:{e['id']}"
        if entry_id in known:
            counts["known"] += 1
            continue
        description = str(e.get("description") or "").strip()
        hub.record_time(entry_id, found[0], found[1], started.isoformat(), max(1, round(duration / 60)),
                        "" if description == project.strip() else description, "", "Toggl")
        counts["imported"] += 1
    return {"status": "done", **counts}
