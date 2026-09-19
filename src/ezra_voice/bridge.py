"""Ezra との橋渡し。依頼を渡し、終わったかどうかを Ezra の記録から見張る。

Slack のトークンは増やさない。同じ Mac の中のファイルと SQLite を読み書きするだけ。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ezra import ask
from ezra.config import Config


@dataclass
class Finished:
    theme: str
    failed: bool

    @property
    def message(self) -> str:
        if self.failed:
            return f"さっき {self.theme} に頼んだ作業、うまくいかなかったみたい。Slack を見てみて。"
        return f"さっき {self.theme} に頼んだ作業、終わったよ。結果は Slack に出てる。"


def send_request(config: Config, theme: str, text: str) -> None:
    ask.write_ask(config, theme, text, kind="request")


def send_note(config: Config, theme: str, text: str) -> None:
    ask.write_ask(config, theme, text, kind="note")


class Watcher:
    """声から出した依頼が終わったかを、Ezra の記録（runs）から見張る。"""

    def __init__(self, config: Config, since: float):
        self.config = config
        self.since = since
        self.seen: set[int] = set()

    def finished(self) -> list[Finished]:
        if not self.config.db_path.exists():
            return []
        conn = sqlite3.connect(f"file:{self.config.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, channel_name, is_error FROM runs "
                "WHERE trigger = 'voice' AND ended_at IS NOT NULL AND started_at >= ? ORDER BY id",
                (self.since,),
            ).fetchall()
        finally:
            conn.close()
        done = []
        for row in rows:
            if row["id"] in self.seen:
                continue
            self.seen.add(row["id"])
            done.append(Finished(row["channel_name"], bool(row["is_error"])))
        return done
