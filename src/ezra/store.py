"""SQLite に保存する状態: スレッドとセッション、ジョブ、実行時間の記録。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    cwd TEXT NOT NULL,
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    pueue_id INTEGER,
    status TEXT NOT NULL,
    detail TEXT,
    submitted_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    reported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notion_links (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    page_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
CREATE TABLE IF NOT EXISTS schedule_runs (
    name TEXT NOT NULL,
    day TEXT NOT NULL,
    ran_at REAL NOT NULL,
    detail TEXT,
    PRIMARY KEY (name, day)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    is_error INTEGER,
    cost_usd REAL
);
"""


@dataclass
class Job:
    id: int
    request_id: str
    channel: str
    thread_ts: str
    cwd: str
    name: str
    command: str
    pueue_id: int | None
    status: str
    detail: str | None
    submitted_at: float
    started_at: float | None
    finished_at: float | None
    reported: int

    def to_state(self) -> dict:
        """テーマのディレクトリに書き出す、Claude から見えるジョブの状態。"""
        return {
            "job_id": self.id,
            "request_id": self.request_id,
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "detail": self.detail,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log": f"logs/job-{self.id}.log",
        }


def _schedule_status(detail: str | None) -> str:
    """schedule_runs の detail に入っている status。読めなければ空文字。"""
    try:
        return str((json.loads(detail or "{}") or {}).get("status", ""))
    except json.JSONDecodeError:
        return ""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(threads)")}
        with self.conn:
            if "awaiting_since" not in columns:
                # Ezra の確認待ちや、失敗したジョブのあとに返事がない状態が始まった時刻
                self.conn.execute("ALTER TABLE threads ADD COLUMN awaiting_since REAL")
            if "nudged" not in columns:
                self.conn.execute("ALTER TABLE threads ADD COLUMN nudged INTEGER NOT NULL DEFAULT 0")

    # threads

    def get_thread(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM threads WHERE channel = ? AND thread_ts = ?", (channel, thread_ts)
        ).fetchone()

    def upsert_thread(self, channel: str, thread_ts: str, channel_name: str, session_id: str | None) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """INSERT INTO threads (channel, thread_ts, channel_name, session_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (channel, thread_ts) DO UPDATE SET
                     session_id = COALESCE(excluded.session_id, threads.session_id),
                     channel_name = excluded.channel_name,
                     updated_at = excluded.updated_at""",
                (channel, thread_ts, channel_name, session_id, now, now),
            )

    def clear_session(self, channel: str, thread_ts: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE threads SET session_id = NULL, updated_at = ? WHERE channel = ? AND thread_ts = ?",
                (time.time(), channel, thread_ts),
            )

    def set_awaiting(self, channel: str, thread_ts: str, awaiting: bool) -> None:
        with self.conn:
            if awaiting:
                self.conn.execute(
                    "UPDATE threads SET awaiting_since = ?, nudged = 0 WHERE channel = ? AND thread_ts = ?",
                    (time.time(), channel, thread_ts),
                )
            else:
                self.conn.execute(
                    "UPDATE threads SET awaiting_since = NULL, nudged = 0 WHERE channel = ? AND thread_ts = ?",
                    (channel, thread_ts),
                )

    def threads_to_nudge(self, older_than: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM threads WHERE awaiting_since IS NOT NULL AND awaiting_since <= ? AND nudged = 0",
            (older_than,),
        ).fetchall()

    def mark_nudged(self, channel: str, thread_ts: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE threads SET nudged = 1 WHERE channel = ? AND thread_ts = ?", (channel, thread_ts))

    def threads_awaiting(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM threads WHERE awaiting_since IS NOT NULL").fetchall()

    def threads_updated_since(self, since: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM threads WHERE updated_at >= ? ORDER BY channel_name, updated_at", (since,)
        ).fetchall()

    def last_activity_by_channel_name(self) -> dict[str, float]:
        rows = self.conn.execute("SELECT channel_name, MAX(updated_at) AS last FROM threads GROUP BY channel_name")
        return {r["channel_name"]: r["last"] for r in rows}

    # Slack のスレッドと Notion のページの対応（振り返りのスレッドなど）

    def link_notion(self, channel: str, thread_ts: str, page_id: str, kind: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO notion_links (channel, thread_ts, page_id, kind) VALUES (?, ?, ?, ?)",
                (channel, thread_ts, page_id, kind),
            )

    def notion_link(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM notion_links WHERE channel = ? AND thread_ts = ?", (channel, thread_ts)
        ).fetchone()

    # schedule

    def schedule_ran(self, name: str, day: str) -> bool:
        """その日の分を実行済みか。途中で中断したものは、まだ実行していないものとして扱う。"""
        row = self.conn.execute(
            "SELECT detail FROM schedule_runs WHERE name = ? AND day = ?", (name, day)
        ).fetchone()
        if row is None:
            return False
        return _schedule_status(row["detail"]) != "interrupted"

    def mark_interrupted_schedules(self) -> list[tuple[str, str]]:
        """前回の起動で「実行中」のまま終わった定期処理を「中断」にする。起動時に1回呼ぶ。

        これをしないと、記録が残っているせいで、その日の分が二度と実行されない。
        """
        rows = self.conn.execute("SELECT name, day, detail FROM schedule_runs").fetchall()
        stuck = [(r["name"], r["day"]) for r in rows if _schedule_status(r["detail"]) == "running"]
        with self.conn:
            for name, day in stuck:
                self.conn.execute(
                    "UPDATE schedule_runs SET detail = ? WHERE name = ? AND day = ?",
                    (json.dumps({"status": "interrupted"}, ensure_ascii=False), name, day),
                )
        return stuck

    def record_schedule(self, name: str, day: str, detail: dict | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO schedule_runs (name, day, ran_at, detail) VALUES (?, ?, ?, ?)",
                (name, day, time.time(), json.dumps(detail or {}, ensure_ascii=False)),
            )

    def last_schedule(self, name: str, before_day: str | None = None) -> sqlite3.Row | None:
        if before_day is None:
            return self.conn.execute(
                "SELECT * FROM schedule_runs WHERE name = ? ORDER BY ran_at DESC LIMIT 1", (name,)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM schedule_runs WHERE name = ? AND day < ? ORDER BY day DESC LIMIT 1", (name, before_day)
        ).fetchone()

    def jobs_finished_since(self, since: float) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE finished_at >= ? ORDER BY finished_at", (since,)
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    # jobs

    def add_job(self, request_id: str, channel: str, thread_ts: str, cwd: str, name: str, command: str,
                status: str, detail: str | None = None) -> Job:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO jobs (request_id, channel, thread_ts, cwd, name, command, status, detail, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, channel, thread_ts, cwd, name, command, status, detail, time.time()),
            )
        return self.get_job(cur.lastrowid)

    def has_request(self, request_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE request_id = ?", (request_id,)).fetchone() is not None

    def get_job(self, job_id: int) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job(**dict(row)) if row else None

    def update_job(self, job_id: int, **fields) -> Job:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
        return self.get_job(job_id)

    def active_jobs(self) -> list[Job]:
        # pueue に投入し終える前の行を混ぜない（pueue_id がまだ空のうちに見ると、失敗と誤判定する）
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running') AND pueue_id IS NOT NULL"
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def unreported_finished_jobs(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled') AND reported = 0"
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    # runs

    def start_run(self, channel: str, thread_ts: str, channel_name: str, trigger: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (channel, thread_ts, channel_name, trigger, started_at) VALUES (?, ?, ?, ?, ?)",
                (channel, thread_ts, channel_name, trigger, time.time()),
            )
        return cur.lastrowid

    def end_run(self, run_id: int, is_error: bool, cost_usd: float | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET ended_at = ?, is_error = ?, cost_usd = ? WHERE id = ?",
                (time.time(), int(is_error), cost_usd, run_id),
            )


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
