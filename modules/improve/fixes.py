"""自己改善の記録（1つの要望 = 1つのスレッド）。モジュールの記録（core.records）の種類 fix、鍵はスレッドの ts。

状態は planning（案を出している）/ working（直している）/ review（取り込み待ち）/ restarting（取り込んで、新しい版での
起動待ち）/ done / failed。直すのは一度に1つだけ。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace

# 直している途中・取り込み待ち・入れ替え待ち。この状態のものがあれば、ほかの直しには着手しない
ACTIVE = ("working", "review", "restarting")


@dataclass(frozen=True)
class Fix:
    channel: str
    thread_ts: str
    # 最初の要望の文（issue の要約の元。Slack にだけ残す）
    request: str
    status: str
    branch: str = ""
    worktree: str = ""
    base_commit: str = ""
    merge_commit: str = ""
    detail: str = ""
    # 要望から作った公開の GitHub issue の番号（まだなら None）
    issue_number: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class Fixes:
    def __init__(self, records):
        self.records = records

    def _save(self, fix: Fix) -> Fix:
        fix = replace(fix, updated_at=time.time())
        self.records.put("fix", fix.thread_ts, asdict(fix))
        return fix

    def get(self, thread_ts: str) -> Fix | None:
        found = self.records.get("fix", thread_ts) if thread_ts else None
        return Fix(**found) if found else None

    def request(self, channel: str, thread_ts: str, request: str, issue_number: int | None = None) -> Fix:
        """要望を受けた（planning）。もうあれば、状態と最初の文はそのままで、issue の番号だけ足す。"""
        fix = self.get(thread_ts)
        if fix is None:
            now = time.time()
            return self._save(Fix(channel, thread_ts, request, "planning", issue_number=issue_number, created_at=now))
        if issue_number is not None:
            fix = replace(fix, issue_number=issue_number)
        return self._save(fix)

    def start(self, channel: str, thread_ts: str, request: str, **values) -> Fix:
        """直し始める（working）。要望の記録が無ければ（前からのスレッドなど）、ここで作る。"""
        fix = self.get(thread_ts) or Fix(channel, thread_ts, request, "working", created_at=time.time())
        return self._save(replace(fix, status="working", **values))

    def update(self, thread_ts: str, **values) -> Fix | None:
        fix = self.get(thread_ts)
        return self._save(replace(fix, **values)) if fix is not None else None

    def in_status(self, *statuses: str) -> list[Fix]:
        """その状態の記録（古い順）。"""
        fixes = (Fix(**value) for value in self.records.items("fix"))
        return sorted((f for f in fixes if f.status in statuses), key=lambda f: f.created_at)
