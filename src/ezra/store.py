"""SQLite に保存する状態: スレッドとセッション、ジョブ、実行時間の記録。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
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


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

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
        rows = self.conn.execute("SELECT * FROM jobs WHERE status IN ('queued', 'running')").fetchall()
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
