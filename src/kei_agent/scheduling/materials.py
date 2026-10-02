"""頭（手の口の読む道具）に渡す材料。Daily・締切の知らせ・振り返りを頭が組み立てるときに読む。

- agenda … これから days 日の授業・会議・締切（各担当の予定。朝の一覧と同じ材料）。時刻の早い順
- reading … モジュールが出す読みもの（知識の担当が朝に出した記事と、👍 したか）
- recent … この hours 時間の動き（やり取りのあったスレッド、担当ごとの実行と失敗、手の口の頼みごと、終わったジョブ）
- jobs … 研究のジョブ（動いているものと、最近終わったもの）

どれも読むだけで、Slack の書式（エスケープ・絵文字）は付けない。スレッドの本文の抜き出しは、研究テーマと研究全体の
スレッドだけ（プロジェクトと担当のスレッドは、時刻と回数だけ。中身は頭が run で担当に聞く）。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent.conversation.dates import parse_time
from kei_agent.framework import modules
from kei_agent.scheduling import deadline
from kei_agent.storage.records import Records
from kei_agent.workspaces import themes
from kei_agent.workspaces.theme_files import thread_log_path
from kei_agent.workspaces.themes import ChannelKind

if TYPE_CHECKING:
    from kei_agent.conversation.assistant import Assistant

# 頭が頼める長さの上限
MAX_AGENDA_DAYS = 14
MAX_READING_DAYS = 7
MAX_RECENT_HOURS = 168
# 終わったジョブを見る日数
JOB_DAYS = 2
# スレッドの本文を抜き出す種類と、その長さ
EXCERPT_KINDS = (ChannelKind.THEME, ChannelKind.OVERVIEW)
REQUEST_CHARS = 200
ANSWER_CHARS = 400
_SECTION = re.compile(r"^## (.+?)（(\d{4}-\d{2}-\d{2} \d{2}:\d{2})）$", re.MULTILINE)


def _clamp(value: int, top: int) -> int:
    return max(1, min(int(value), top))


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ticket_status(ticket: dict, running: set[str]) -> str:
    """受付番号の様子。running のまま本体が起動し直したものは failed（Hands.status と同じ）。"""
    status = str(ticket.get("status") or "")
    return "failed" if status == "running" and ticket.get("ticket") not in running else status


def _stamp(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") if value else ""


# 予定

async def agenda(assistant: Assistant, days: int, now: datetime | None = None) -> dict:
    days = _clamp(days, MAX_AGENDA_DAYS)
    now = now or datetime.now()
    first, last = now.date(), now.date() + timedelta(days=days - 1)
    found, unread = await assistant.module_agenda(days)
    items: list[dict] = []
    for name, entries in found.items():
        agent = modules.known()[name].label
        for item in entries:
            kind = item.get("kind", "meeting")
            if kind == "due":
                at = parse_time(item.get("at", ""))
                if at is None or not first <= deadline.day(at) <= last:
                    continue
                items.append({"kind": "締切", "date": f"{deadline.day(at):%Y-%m-%d}", "start": deadline.clock(at),
                              "end": "", "title": str(item.get("title") or ""), "course": str(item.get("course") or ""),
                              "url": str(item.get("url") or ""), "agent": agent, "_at": at})
                continue
            start, end = parse_time(item.get("start", "")), parse_time(item.get("end", ""))
            if start is None or not first <= start.date() <= last:
                continue
            items.append({"kind": "授業" if kind == "class" else "会議", "date": f"{start:%Y-%m-%d}",
                          "start": f"{start:%H:%M}", "end": f"{end:%H:%M}" if end else "",
                          "title": str(item.get("subject") or item.get("title") or ""),
                          "where": str(item.get("location") or ""), "agent": agent, "_at": start})
    items.sort(key=lambda item: item.pop("_at"))
    return {"from": f"{first:%Y-%m-%d}", "to": f"{last:%Y-%m-%d}", "items": items, "unread": unread}


# 読みもの

async def reading(assistant: Assistant, days: int) -> dict:
    found = await assistant.module_head_materials(_clamp(days, MAX_READING_DAYS))
    return {"items": found.get("reading", [])}


# 最近の動き

def _excerpt(path: Path) -> dict:
    """スレッドの記録から、最初の頼みごとと最後の Kei Agent の答え（どちらも短く）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    marks = list(_SECTION.finditer(text))
    sections = [(m.group(1), text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip())
                for i, m in enumerate(marks)]
    asked = next((body for who, body in sections if not who.startswith("Kei Agent")), "")
    answered = next((body for who, body in reversed(sections) if who.startswith("Kei Agent")), "")
    return {"request": asked[:REQUEST_CHARS], "answer": answered[:ANSWER_CHARS]}


def recent(assistant: Assistant, hours: int, now: float | None = None) -> dict:
    hours = _clamp(hours, MAX_RECENT_HOURS)
    now = time.time() if now is None else now
    since = now - hours * 3600
    config, store = assistant.config, assistant.store
    threads = []
    for row in store.threads_updated_since(since):
        try:
            ws = themes.resolve(config, row["channel_name"])
        except ValueError:
            continue
        item = {"workspace": row["channel_name"], "updated": _stamp(row["updated_at"]),
                "started": _stamp(_float(row["thread_ts"])), "from": "手の口" if row["channel"] == "mcp" else "Slack"}
        if ws.kind in EXCERPT_KINDS and ws.cwd is not None:
            item |= _excerpt(thread_log_path(ws.cwd, row["thread_ts"]))
        threads.append(item)
    runs: dict[str, Counter] = {}
    for row in store.runs_since(since):
        counted = runs.setdefault(row["actor"] or row["channel_name"], Counter())
        counted["runs"] += 1
        counted["failed"] += int(bool(row["is_error"]))
        counted["running"] += int(row["ended_at"] is None)
    running = set(assistant.hands_tasks)
    asked = [{key: ticket.get(key) for key in ("workspace", "conversation")}
             | {"status": _ticket_status(ticket, running), "started": _stamp(ticket.get("started_at"))}
             for ticket in Records(store, "hands").items("ticket") if (_float(ticket.get("started_at")) or 0) >= since]
    finished = [_job(job) for job in store.jobs_finished_since(since)]
    return {"since": _stamp(since), "threads": threads,
            "runs": [{"agent": name, **counted} for name, counted in sorted(runs.items())],
            "hands": asked, "jobs": finished}


# ジョブ

def _job(job) -> dict:
    return {"id": job.id, "name": job.name, "workspace": Path(job.cwd).name, "status": job.status,
            "detail": job.detail or "", "submitted": _stamp(job.submitted_at), "started": _stamp(job.started_at),
            "finished": _stamp(job.finished_at)}


def jobs(assistant: Assistant, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    store = assistant.store
    return {"active": [_job(job) for job in store.active_jobs()],
            "finished": [_job(job) for job in store.jobs_finished_since(now - JOB_DAYS * 86400)]}
