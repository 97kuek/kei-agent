"""SQLite に保存する状態: スレッドとセッション、ジョブ、実行時間の記録。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields
from pathlib import Path

# runs を残す最短の日数。timelog の週ごとの集計で前の週と比べるので、8 週より短くしない
RUNS_MIN_DAYS = 8 * 7

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
-- 契約の上限に達して、あとでやり直すもの（assistant.py / schedule.py）
CREATE TABLE IF NOT EXISTS deferred_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,        -- request（上限で止まった依頼） / schedule（決まった時刻の処理）
                               -- / in_flight（いま処理中の依頼。終われば消す。残っていたら再起動で中断されたもの）
    payload TEXT NOT NULL,     -- JSON
    run_after REAL NOT NULL,
    created_at REAL NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
-- Kei Agent 自身を直す流れ（improve.py）
CREATE TABLE IF NOT EXISTS improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL UNIQUE,
    request TEXT NOT NULL,
    -- planning（案を出している） / working（直している） / review（取り込み待ち）
    -- / restarting（取り込んで再起動待ち） / done / failed
    status TEXT NOT NULL,
    branch TEXT,
    worktree TEXT,
    base_commit TEXT,
    merge_commit TEXT,
    detail TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- Slack から変える設定（settings.py）
CREATE TABLE IF NOT EXISTS theme_domains (
    theme TEXT NOT NULL,
    domain TEXT NOT NULL,
    reason TEXT,
    added_at REAL NOT NULL,
    PRIMARY KEY (theme, domain)
);
CREATE TABLE IF NOT EXISTS domain_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    theme TEXT NOT NULL,
    domain TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    -- 決めた内容を Claude に伝えて作業を再開したか
    resumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- エージェントが持っている会話の続き（どのスレッドの、どのエージェントの claude か）。
-- 会話の単位はスレッドなので、本体が覚える（docs/architecture.md の「振り分けと A2A」）
CREATE TABLE IF NOT EXISTS agent_sessions (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts, agent)
);
-- 新しい会話の鍵は provider と指示版まで揃うときだけ再利用する。
-- 旧 threads / agent_sessions の session_id は migration 後も消さない。
CREATE TABLE IF NOT EXISTS provider_sessions (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts, agent, provider)
);
CREATE TABLE IF NOT EXISTS provider_thread_state (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts, agent)
);
CREATE TABLE IF NOT EXISTS provider_limits (
    provider TEXT PRIMARY KEY,
    until REAL NOT NULL
);
-- 一度だけ知らせるもの（課題の締切 24 時間前など）。同じ目印では二度知らせない
CREATE TABLE IF NOT EXISTS notices (
    key TEXT PRIMARY KEY,
    at REAL NOT NULL
);
-- Slack の時間記録。人の計測は runs（Kei Agent 自身の実行時間）と混ぜない
CREATE TABLE IF NOT EXISTS time_cards (
    channel TEXT PRIMARY KEY,
    message_ts TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS course_channel_bindings (
    channel TEXT PRIMARY KEY,
    course_page_id TEXT NOT NULL,
    course_name TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS time_entries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    domain TEXT NOT NULL CHECK(domain IN ('research', 'course', 'work')),
    channel TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    course_page_id TEXT NOT NULL DEFAULT '',
    course_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at REAL,
    toggl_state TEXT NOT NULL DEFAULT 'pending',
    notion_state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS active_timers (
    user_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(entry_id) REFERENCES time_entries(id)
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
    # ジョブが作るはずのファイル（テーマのディレクトリからの相対パス）。JSON の配列
    expects: str | None = None

    @property
    def expected_files(self) -> list[str]:
        try:
            return [str(p) for p in json.loads(self.expects or "[]")]
        except json.JSONDecodeError:
            return []

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        """jobs の行を Job にする。知らない列は無視する（列を足しても壊れない）。"""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(row).items() if k in names})

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
            "expects": self.expected_files,
        }


def _schedule_status(detail: str | None) -> str:
    """schedule_runs の detail に入っている status。読めなければ空文字。"""
    try:
        return str((json.loads(detail or "{}") or {}).get("status", ""))
    except json.JSONDecodeError:
        return ""


# あとから足した列。既存のデータベースにも同じ形を用意する
ADDED_COLUMNS = {
    # ジョブが作るはずのファイル（JSON の配列）。終わったときに、あるかどうかを確かめる
    "jobs": {"expects": "TEXT"},
    # Kei Agent の確認待ちや、失敗したジョブのあとに返事がない状態が始まった時刻
    "threads": {"awaiting_since": "REAL", "nudged": "INTEGER NOT NULL DEFAULT 0",
                # この会話に渡した prompts/system.md の版（runner.system_prompt_version）
                "prompt_version": "TEXT",
                # エラーや上限で止まった依頼の文。次の依頼に文脈として渡す
                "stalled_request": "TEXT",
                # 依頼者の依頼の数と、最後に「新しいスレッドで続ける」を出したときの数（handoff.py）
                "turns": "INTEGER NOT NULL DEFAULT 0", "handoff_offered_at": "INTEGER NOT NULL DEFAULT 0",
                # 前のスレッドからの引き継ぎメモ。このスレッドの最初の回に渡す
                "handoff_memo": "TEXT",
                # 区切って引き継いだ先のスレッド
                "handed_off_to": "TEXT"},
}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # バックアップが別のコネクションから読むので、読み書きがぶつからないようにする
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        with self.conn:
            for table, columns in ADDED_COLUMNS.items():
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, kind in columns.items():
                    if name not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def snapshot(self, path: Path) -> None:
        """いまのデータベースを、書き込みと混ざらない形で別ファイルに写す。

        毎晩の保守は別スレッドから呼ぶ。sqlite3 の接続は作ったスレッドでしか使えないので、
        ここで読み取り用につなぎ直す（WAL を使っているため、ファイルのコピーでは中身がそろわない）。
        """
        reader = sqlite3.connect(self.path)
        try:
            dest = sqlite3.connect(path)
            try:
                reader.backup(dest)
            finally:
                dest.close()
        finally:
            reader.close()

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

    # 上限に達して、あとでやり直すもの

    def defer_run(self, kind: str, payload: dict, run_after: float) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO deferred_runs (kind, payload, run_after, created_at) VALUES (?, ?, ?, ?)",
                (kind, json.dumps(payload, ensure_ascii=False), run_after, time.time()),
            )
        return int(cur.lastrowid)

    def due_deferred(self, kind: str, now: float) -> list[tuple[int, dict]]:
        rows = self.conn.execute(
            "SELECT id, payload FROM deferred_runs WHERE kind = ? AND done = 0 AND run_after <= ? ORDER BY id",
            (kind, now),
        ).fetchall()
        return [(r["id"], json.loads(r["payload"])) for r in rows]

    def start_in_flight(self, payload: dict) -> int:
        """処理中の依頼として控える。終わったら finish_deferred で消す。"""
        return self.defer_run("in_flight", payload, 0)

    def interrupted_requests(self) -> list[tuple[int, dict]]:
        """前回の終了時に処理中だった依頼（再起動や強制終了で中断されたもの）。"""
        return self.due_deferred("in_flight", time.time())

    def end_open_runs(self) -> int:
        """終わりが記録されていない実行を、止まったものとして閉じる（研究時間の集計がずれないように）。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE runs SET ended_at = ?, is_error = 1 WHERE ended_at IS NULL", (time.time(),))
        return cur.rowcount

    def pending_deferred(self, kind: str) -> list[tuple[int, dict]]:
        """まだやり直していないもの（時刻が来ているかは見ない）。"""
        rows = self.conn.execute(
            "SELECT id, payload FROM deferred_runs WHERE kind = ? AND done = 0 ORDER BY id", (kind,)).fetchall()
        return [(r["id"], json.loads(r["payload"])) for r in rows]

    def finish_deferred(self, deferred_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE deferred_runs SET done = 1 WHERE id = ?", (deferred_id,))

    # Kei Agent 自身を直す流れ（improve.py）

    def improvement(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM improvements WHERE channel = ? AND thread_ts = ?", (channel, thread_ts)).fetchone()

    def improvement_by_thread(self, thread_ts: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM improvements WHERE thread_ts = ?", (thread_ts,)).fetchone()

    def improvements_in(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ", ".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM improvements WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()

    def start_improvement(self, channel: str, thread_ts: str, request: str, **values) -> sqlite3.Row:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """INSERT INTO improvements (channel, thread_ts, request, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'working', ?, ?)
                   ON CONFLICT (thread_ts) DO UPDATE SET status = 'working', updated_at = excluded.updated_at""",
                (channel, thread_ts, request, now, now),
            )
        return self.update_improvement(channel, thread_ts, **values)

    def update_improvement(self, channel: str, thread_ts: str, **values) -> sqlite3.Row:
        if values:
            sets = ", ".join(f"{k} = ?" for k in values)
            with self.conn:
                self.conn.execute(
                    f"UPDATE improvements SET {sets}, updated_at = ? WHERE channel = ? AND thread_ts = ?",
                    (*values.values(), time.time(), channel, thread_ts),
                )
        return self.improvement(channel, thread_ts)

    def set_prompt_version(self, channel: str, thread_ts: str, version: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE threads SET prompt_version = ? WHERE channel = ? AND thread_ts = ?",
                              (version, channel, thread_ts))

    def set_stalled(self, channel: str, thread_ts: str, text: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE threads SET stalled_request = ? WHERE channel = ? AND thread_ts = ?",
                              (text, channel, thread_ts))

    def count_turn(self, channel: str, thread_ts: str) -> int:
        """依頼者の依頼を1つ数え、これまでの数を返す。"""
        with self.conn:
            self.conn.execute("UPDATE threads SET turns = turns + 1 WHERE channel = ? AND thread_ts = ?",
                              (channel, thread_ts))
        row = self.get_thread(channel, thread_ts)
        return row["turns"] if row else 0

    def update_thread(self, channel: str, thread_ts: str, **values) -> None:
        """引き継ぎの欄（handoff_offered_at、handoff_memo、handed_off_to）を書き換える。"""
        allowed = {"handoff_offered_at", "handoff_memo", "handed_off_to"}
        if not values or not set(values) <= allowed:
            raise ValueError(f"書き換えられない欄です: {sorted(set(values) - allowed)}")
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.conn:
            self.conn.execute(f"UPDATE threads SET {sets} WHERE channel = ? AND thread_ts = ?",
                              (*values.values(), channel, thread_ts))

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

    def forget_stale_awaits(self, before: float) -> int:
        """声をかけても返事がないまま古くなった返事待ちを、いったん閉じる。

        閉じないと、App Home の「いま動いているもの」に何日も居座り続ける。返信すれば
        またスレッドの続きとして動くので、ここで忘れても取り返しはつく。
        """
        with self.conn:
            cur = self.conn.execute(
                "UPDATE threads SET awaiting_since = NULL, nudged = 0 "
                "WHERE awaiting_since IS NOT NULL AND awaiting_since <= ? AND nudged = 1",
                (before,),
            )
        return cur.rowcount

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

    def relink_notion_pages(self, old_to_new: dict[str, str]) -> int:
        """全件の移行先が照合できた後、既存スレッドのリンク先を一括更新する。"""
        if any(not old or not new for old, new in old_to_new.items()):
            raise ValueError("元 ID と移行先 ID が必要です")
        with self.conn:
            count = 0
            for old, new in old_to_new.items():
                count += self.conn.execute(
                    "UPDATE notion_links SET page_id = ? WHERE page_id = ?", (new, old)
                ).rowcount
        return count

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

    # エージェントの会話の続き

    def last_provider(self, channel: str, thread_ts: str, agent: str) -> str | None:
        row = self.conn.execute(
            "SELECT provider FROM provider_thread_state WHERE channel = ? AND thread_ts = ? AND agent = ?",
            (channel, thread_ts, agent),
        ).fetchone()
        return row["provider"] if row else None

    def session_for(self, channel: str, thread_ts: str, agent: str,
                    provider: str, prompt_version: str) -> str | None:
        if self.last_provider(channel, thread_ts, agent) != provider:
            return None
        row = self.conn.execute(
            """SELECT session_id FROM provider_sessions
               WHERE channel = ? AND thread_ts = ? AND agent = ?
                 AND provider = ? AND prompt_version = ?""",
            (channel, thread_ts, agent, provider, prompt_version),
        ).fetchone()
        return row["session_id"] if row else None

    def set_last_provider(self, channel: str, thread_ts: str, agent: str, provider: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO provider_thread_state (channel, thread_ts, agent, provider, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (channel, thread_ts, agent) DO UPDATE SET
                     provider = excluded.provider, updated_at = excluded.updated_at""",
                (channel, thread_ts, agent, provider, time.time()),
            )

    def set_session(self, channel: str, thread_ts: str, agent: str, provider: str,
                    session_id: str, prompt_version: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO provider_sessions
                   (channel, thread_ts, agent, provider, session_id, prompt_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (channel, thread_ts, agent, provider) DO UPDATE SET
                     session_id = excluded.session_id, prompt_version = excluded.prompt_version,
                     updated_at = excluded.updated_at""",
                (channel, thread_ts, agent, provider, session_id, prompt_version, time.time()),
            )
            self.conn.execute(
                """INSERT INTO provider_thread_state (channel, thread_ts, agent, provider, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (channel, thread_ts, agent) DO UPDATE SET
                     provider = excluded.provider, updated_at = excluded.updated_at""",
                (channel, thread_ts, agent, provider, time.time()),
            )

    def limit_until(self, provider: str) -> float:
        row = self.conn.execute("SELECT until FROM provider_limits WHERE provider = ?", (provider,)).fetchone()
        return float(row["until"]) if row else 0.0

    def set_limit_until(self, provider: str, until: float) -> None:
        if provider not in {"claude", "codex"}:
            raise ValueError(f"unknown provider: {provider}")
        with self.conn:
            self.conn.execute(
                """INSERT INTO provider_limits (provider, until) VALUES (?, ?)
                   ON CONFLICT (provider) DO UPDATE SET until = excluded.until""",
                (provider, until),
            )

    def agent_session(self, channel: str, thread_ts: str, agent: str) -> str | None:
        row = self.conn.execute(
            "SELECT session_id FROM agent_sessions WHERE channel = ? AND thread_ts = ? AND agent = ?",
            (channel, thread_ts, agent)).fetchone()
        return row["session_id"] if row else None

    def set_agent_session(self, channel: str, thread_ts: str, agent: str, session_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO agent_sessions (channel, thread_ts, agent, session_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (channel, thread_ts, agent, session_id, time.time()))

    def thread_agent(self, channel: str, thread_ts: str) -> str | None:
        """そのスレッドに最後に答えたエージェント（会話の続きを同じ相手に回すのに使う）。

        会話の鍵（session_id）を持たないエージェントもいるので、鍵の有無では判断しない。
        """
        row = self.conn.execute(
            "SELECT agent FROM agent_sessions WHERE channel = ? AND thread_ts = ? "
            "ORDER BY updated_at DESC LIMIT 1", (channel, thread_ts)).fetchone()
        return row["agent"] if row else None

    # Slack から変える設定（settings.py）

    def theme_domains(self, theme: str | None = None) -> list[sqlite3.Row]:
        if theme is None:
            return self.conn.execute("SELECT theme, domain FROM theme_domains ORDER BY theme, domain").fetchall()
        return self.conn.execute(
            "SELECT theme, domain FROM theme_domains WHERE theme = ? ORDER BY domain", (theme,)).fetchall()

    def add_theme_domain(self, theme: str, domain: str, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO theme_domains (theme, domain, reason, added_at) VALUES (?, ?, ?, ?)",
                (theme, domain, reason, time.time()),
            )

    def remove_theme_domains(self, theme: str, domain: str | None = None) -> None:
        """domain を省くと、そのテーマの接続先をすべて消す。"""
        with self.conn:
            if domain is None:
                self.conn.execute("DELETE FROM theme_domains WHERE theme = ?", (theme,))
            else:
                self.conn.execute("DELETE FROM theme_domains WHERE theme = ? AND domain = ?", (theme, domain))

    def add_domain_request(self, channel: str, thread_ts: str, theme: str, domain: str, reason: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO domain_requests (channel, thread_ts, theme, domain, reason, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (channel, thread_ts, theme, domain, reason, time.time()),
            )
        return int(cur.lastrowid)

    def domain_request(self, request_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM domain_requests WHERE id = ?", (request_id,)).fetchone()

    def resolve_domain_request(self, request_id: int, status: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE domain_requests SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (status, time.time(), request_id),
            )
        return cur.rowcount == 1

    def count_pending_domain_requests(self, channel: str, thread_ts: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM domain_requests WHERE channel = ? AND thread_ts = ? AND status = 'pending'",
            (channel, thread_ts)).fetchone()
        return int(row["n"])

    def take_domain_decisions(self, channel: str, thread_ts: str) -> list[sqlite3.Row]:
        with self.conn:
            rows = self.conn.execute(
                """SELECT * FROM domain_requests WHERE channel = ? AND thread_ts = ?
                   AND status != 'pending' AND resumed = 0 ORDER BY id""", (channel, thread_ts)).fetchall()
            self.conn.execute(
                "UPDATE domain_requests SET resumed = 1 WHERE channel = ? AND thread_ts = ? AND status != 'pending'",
                (channel, thread_ts))
        return rows

    def setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def delete_setting(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # 一度だけ知らせるもの

    def noticed(self, key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM notices WHERE key = ?", (key,)).fetchone()
        return row is not None

    def record_notice(self, key: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO notices (key, at) VALUES (?, ?)", (key, time.time()))

    def drop_old_agent_sessions(self, before: float) -> int:
        """古いスレッドのエージェントの会話と、済んだ記録を忘れる（毎晩の保守から呼ぶ）。消した行数を返す。

        runs は研究時間の週ごとの集計に使うので、before より新しくても最低 RUNS_MIN_DAYS 日は残す。
        schedule_runs は「前回からの差分」に使うので、処理ごとに最新の1行は残す。
        """
        runs_before = min(before, time.time() - RUNS_MIN_DAYS * 86400)
        with self.conn:
            removed = 0
            for sql, args in (
                ("DELETE FROM agent_sessions WHERE updated_at < ?", (before,)),
                ("DELETE FROM provider_sessions WHERE updated_at < ?", (before,)),
                ("DELETE FROM provider_thread_state WHERE updated_at < ?", (before,)),
                ("DELETE FROM deferred_runs WHERE done = 1 AND created_at < ?", (before,)),
                ("""DELETE FROM schedule_runs WHERE ran_at < ? AND ran_at < (
                        SELECT MAX(ran_at) FROM schedule_runs AS latest WHERE latest.name = schedule_runs.name)""",
                 (before,)),
                ("DELETE FROM runs WHERE ended_at IS NOT NULL AND started_at < ?", (runs_before,)),
            ):
                removed += self.conn.execute(sql, args).rowcount
            return removed

    def drop_old_notices(self, before: float) -> int:
        """古い目印を捨てる（毎晩の保守から呼ぶ）。"""
        with self.conn:
            return self.conn.execute("DELETE FROM notices WHERE at < ?", (before,)).rowcount

    def forget_schedule(self, name: str, day: str) -> None:
        """その日の分の記録を消す（上限に当たったときなど、やり直せるようにする）。"""
        with self.conn:
            self.conn.execute("DELETE FROM schedule_runs WHERE name = ? AND day = ?", (name, day))

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
        return [Job.from_row(r) for r in rows]

    # jobs

    def add_job(self, request_id: str, channel: str, thread_ts: str, cwd: str, name: str, command: str,
                status: str, detail: str | None = None, expects: list[str] | None = None) -> Job:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO jobs (request_id, channel, thread_ts, cwd, name, command, status, detail,
                                     submitted_at, expects)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, channel, thread_ts, cwd, name, command, status, detail, time.time(),
                 json.dumps(expects or [], ensure_ascii=False)),
            )
        job = self.get_job(cur.lastrowid)
        assert job is not None  # 直前に入れた行
        return job

    def has_request(self, request_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE request_id = ?", (request_id,)).fetchone() is not None

    def get_job(self, job_id: int) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def update_job(self, job_id: int, **values) -> Job:
        """jobs の列を書き換える。列名は SQL に埋めるので、Job にある名前だけを許す。"""
        unknown = sorted(set(values) - {f.name for f in fields(Job)})
        if unknown:
            raise ValueError(f"jobs にない列です: {', '.join(unknown)}")
        if values:
            cols = ", ".join(f"{k} = ?" for k in values)
            with self.conn:
                self.conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*values.values(), job_id))
        job = self.get_job(job_id)
        assert job is not None
        return job

    def active_jobs(self) -> list[Job]:
        # pueue に投入し終える前の行を混ぜない（pueue_id がまだ空のうちに見ると、失敗と誤判定する）
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running') AND pueue_id IS NOT NULL"
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def unsubmitted_jobs(self, before: float) -> list[Job]:
        """queued のまま pueue_id が空で、before より前に作った行（投入の途中で止まったもの）。"""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' AND pueue_id IS NULL AND submitted_at < ?", (before,)
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def unreported_finished_jobs(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled') AND reported = 0"
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    # runs

    def start_run(self, channel: str, thread_ts: str, channel_name: str, trigger: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (channel, thread_ts, channel_name, trigger, started_at) VALUES (?, ?, ?, ?, ?)",
                (channel, thread_ts, channel_name, trigger, time.time()),
            )
        return cur.lastrowid

    def open_runs(self) -> list[sqlite3.Row]:
        """終わりがまだ記録されていない実行（いま動いているもの）。古い順。"""
        return self.conn.execute("SELECT * FROM runs WHERE ended_at IS NULL ORDER BY started_at").fetchall()

    def finished_runs(self, since: float, until: float) -> list[sqlite3.Row]:
        """since 以上 until 未満に始まり、終わりが記録された実行。"""
        return self.conn.execute(
            "SELECT channel_name, started_at, ended_at FROM runs "
            "WHERE ended_at IS NOT NULL AND started_at >= ? AND started_at < ?",
            (since, until),
        ).fetchall()

    def end_run(self, run_id: int, is_error: bool, cost_usd: float | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET ended_at = ?, is_error = ?, cost_usd = ? WHERE id = ?",
                (time.time(), int(is_error), cost_usd, run_id),
            )

    # 人の時間記録（Toggl と Notion の同期前の正本）

    def start_time_entry(self, entry_id: str, user_id: str, domain: str, channel: str, channel_name: str,
                         course_page_id: str, course_name: str, description: str, started_at: float,
                         notion_state: str) -> tuple[sqlite3.Row, sqlite3.Row | None]:
        """開始と、前の計測の確定を1つのトランザクションで行う。"""
        with self.conn:
            previous = self.conn.execute(
                """SELECT e.* FROM time_entries e JOIN active_timers a ON a.entry_id = e.id
                   WHERE a.user_id = ?""", (user_id,)).fetchone()
            if previous is not None:
                self.conn.execute(
                    "UPDATE time_entries SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (started_at, previous["id"]),
                )
                self.conn.execute("DELETE FROM active_timers WHERE user_id = ?", (user_id,))
            self.conn.execute(
                """INSERT INTO time_entries
                   (id, user_id, domain, channel, channel_name, course_page_id, course_name, description,
                    started_at, toggl_state, notion_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (entry_id, user_id, domain, channel, channel_name, course_page_id, course_name, description,
                 started_at, notion_state),
            )
            self.conn.execute("INSERT INTO active_timers (user_id, entry_id) VALUES (?, ?)", (user_id, entry_id))
        return self.time_entry(entry_id), self.time_entry(previous["id"]) if previous is not None else None

    def finish_time_entry(self, user_id: str, ended_at: float) -> sqlite3.Row | None:
        """この利用者の計測を閉じる。二度目の停止は何もしない。"""
        with self.conn:
            row = self.active_time_entry(user_id)
            if row is None:
                return None
            self.conn.execute("UPDATE time_entries SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                              (ended_at, row["id"]))
            self.conn.execute("DELETE FROM active_timers WHERE user_id = ?", (user_id,))
        return self.time_entry(row["id"])

    def time_entry(self, entry_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM time_entries WHERE id = ?", (entry_id,)).fetchone()

    def active_time_entry(self, user_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT e.* FROM time_entries e JOIN active_timers a ON a.entry_id = e.id WHERE a.user_id = ?",
            (user_id,),
        ).fetchone()

    def set_time_memo(self, entry_id: str, memo: str) -> sqlite3.Row:
        with self.conn:
            self.conn.execute("UPDATE time_entries SET memo = ? WHERE id = ?", (memo, entry_id))
        row = self.time_entry(entry_id)
        if row is None:
            raise ValueError("時間記録がありません")
        return row

    def set_time_delivery(self, entry_id: str, *, toggl_state: str | None = None,
                          notion_state: str | None = None) -> sqlite3.Row:
        values = {"toggl_state": toggl_state, "notion_state": notion_state}
        values = {key: value for key, value in values.items() if value is not None}
        if values:
            sets = ", ".join(f"{key} = ?" for key in values)
            with self.conn:
                self.conn.execute(f"UPDATE time_entries SET {sets} WHERE id = ?", (*values.values(), entry_id))
        row = self.time_entry(entry_id)
        if row is None:
            raise ValueError("時間記録がありません")
        return row

    def pending_time_entries(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM time_entries WHERE ended_at IS NOT NULL
               AND (toggl_state = 'pending' OR notion_state = 'pending') ORDER BY started_at"""
        ).fetchall()

    def upsert_time_card(self, channel: str, message_ts: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO time_cards (channel, message_ts, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(channel) DO UPDATE SET message_ts = excluded.message_ts, updated_at = excluded.updated_at""",
                (channel, message_ts, time.time()),
            )

    def time_card(self, channel: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM time_cards WHERE channel = ?", (channel,)).fetchone()

    def bind_course_channel(self, channel: str, course_page_id: str, course_name: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO course_channel_bindings (channel, course_page_id, course_name, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(channel) DO UPDATE SET course_page_id = excluded.course_page_id,
                       course_name = excluded.course_name, updated_at = excluded.updated_at""",
                (channel, course_page_id, course_name, time.time()),
            )

    def course_channel_binding(self, channel: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM course_channel_bindings WHERE channel = ?", (channel,)).fetchone()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
