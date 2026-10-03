"""時間の計測の記録（1人1本）。モジュールの記録（core.records）に置く。

種類と鍵:
- `entry` / 記録の id … 1回の計測。止めたあとは、Toggl と共通ホームへ送れたかも持つ。送り終えたら KEEP_DAYS で消える
  （そのころには Toggl と「時間記録」に残っている）
- `active` / 利用者 … いま計測中の記録の id
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, replace

# 領域 → Toggl のプロジェクト名と、共通ホームの「時間記録」の領域の書き方
DOMAINS = {"research": "研究", "course": "大学", "work": "仕事"}
# 送り終えた記録を残す日数（Toggl で直接測った記録の取り込みで、Slack で測った分と見分けるのに使う）
KEEP_DAYS = 30
# 送り終えた状態（needs_review は、利用者が Toggl を確かめて再送するまで待つ）
TOGGL_SENT = ("done", "not_configured")


def project_label(domain: str, label: str) -> str:
    """Toggl のプロジェクト名と記録の説明（研究 / vlm など）。"""
    return f"{DOMAINS[domain]} / {label.strip()}"


@dataclass(frozen=True)
class Entry:
    id: str
    user_id: str
    domain: str
    channel: str
    # 記録のテーマ。course_* は保存済みの科目情報も読み込める形で持つ
    channel_name: str
    course_page_id: str
    course_name: str
    description: str
    memo: str
    started_at: float
    ended_at: float | None
    toggl_state: str = "pending"
    notion_state: str = "pending"

    @property
    def label(self) -> str:
        """共通ホームの「時間記録」のテーマ（大学は選んだ科目、ほかはチャンネルのテーマ）。"""
        return self.course_name if self.domain == "course" and self.course_name else self.channel_name

    @property
    def minutes(self) -> int:
        return max(1, int(((self.ended_at or self.started_at) - self.started_at + 59) // 60))

    @property
    def sent(self) -> bool:
        return self.toggl_state in TOGGL_SENT and self.notion_state == "done"


class Entries:
    """計測の開始・停止・メモ・送り先の状態。records は core.records。"""

    def __init__(self, records):
        self.records = records

    def _save(self, entry: Entry) -> Entry:
        self.records.put("entry", entry.id, asdict(entry), keep_days=KEEP_DAYS if entry.sent else None)
        return entry

    def entry(self, entry_id: str) -> Entry | None:
        found = self.records.get("entry", entry_id) if entry_id else None
        return Entry(**found) if found else None

    def active(self, user_id: str) -> Entry | None:
        found = self.records.get("active", user_id) if user_id else None
        return self.entry(found["entry"]) if found else None

    def start(self, user_id: str, domain: str, channel: str, theme: str,
              started_at: float | None = None, label: str = "") -> tuple[Entry, Entry | None]:
        """計測を始める。前の計測が動いていれば、その時刻で止める（止めた記録も返す）。

        label を渡せば、それを記録の名前にする。
        """
        if not user_id or not channel or not theme or domain not in DOMAINS:
            raise ValueError("利用者・チャンネル・領域を指定してください")
        at = time.time() if started_at is None else started_at
        previous = self.stop(user_id, ended_at=at)
        label = label.strip() or theme
        course_name = label if domain == "course" else ""
        entry = self._save(Entry(uuid.uuid4().hex, user_id, domain, channel, label, "", course_name,
                                 project_label(domain, label), "", at, None))
        self.records.put("active", user_id, {"entry": entry.id})
        return entry, previous

    def stop(self, user_id: str, ended_at: float | None = None) -> Entry | None:
        """この利用者の計測を止める。動いていなければ何もしない。"""
        entry = self.active(user_id)
        if user_id:
            self.records.delete("active", user_id)
        if entry is None:
            return None
        if entry.ended_at is None:
            entry = self._save(replace(entry, ended_at=time.time() if ended_at is None else ended_at))
        return entry

    def add_memo(self, entry_id: str, memo: str) -> Entry:
        entry = self.entry(entry_id)
        if entry is None:
            raise ValueError("時間記録がありません")
        text = "\n".join(part for part in (entry.memo, memo.strip()) if part)
        if len(text) > 1000:
            raise ValueError("メモは合計1000文字までです")
        return self._save(replace(entry, memo=text, notion_state="pending"))

    def set_delivery(self, entry_id: str, *, toggl_state: str | None = None, notion_state: str | None = None) -> Entry:
        entry = self.entry(entry_id)
        if entry is None:
            raise ValueError("時間記録がありません")
        return self._save(replace(entry, toggl_state=toggl_state or entry.toggl_state,
                                  notion_state=notion_state or entry.notion_state))

    def finished(self, since: float = 0, until: float = float("inf")) -> list[Entry]:
        """止めた記録のうち、since〜until に始めたもの（古い順）。"""
        entries = (Entry(**value) for value in self.records.items("entry"))
        return sorted((e for e in entries if e.ended_at is not None and since <= e.started_at < until),
                      key=lambda e: e.started_at)

    def pending(self) -> list[Entry]:
        """止めたのに、Toggl か共通ホームへまだ送れていない記録（古い順）。"""
        return [e for e in self.finished() if "pending" in (e.toggl_state, e.notion_state)]
