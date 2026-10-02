"""時間の計測の記録（1人1本）。モジュールの記録（core.records）に置く。

種類と鍵:
- `entry` / 記録の id … 1回の計測。止めたあとは、Toggl と共通ホームへ送れたかも持つ。送り終えたら KEEP_DAYS で消える
  （そのころには Toggl と「時間記録」に残っている）
- `active` / 利用者 … いま計測中の記録の id
- `card` / チャンネル … 固定した時間記録カードの投稿（ts と、描いたときのボタンの名前）
- `course` / チャンネル … そのチャンネルで選んだ科目（名前が変わっても同じ科目を指す）
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


def prefixes_of(value: object) -> dict[str, str]:
    """設定の prefixes（チャンネル名の頭 → 領域）を確かめて、長い頭から並べる。書き間違いは起動のときに止める。"""
    if not isinstance(value, dict) or not value:
        raise ValueError("config.toml の [time] prefixes は、チャンネル名の頭 → 領域の表にしてください")
    wrong = sorted(f"{k} = {v}" for k, v in value.items() if not str(k) or v not in DOMAINS)
    if wrong:
        raise ValueError(f"config.toml の [time] prefixes の領域は {' / '.join(DOMAINS)} のどれかにしてください: "
                         + "、".join(wrong))
    return dict(sorted(((str(k), str(v)) for k, v in value.items()), key=lambda kv: -len(kv[0])))


def domain_of(prefixes: dict[str, str], channel_name: str) -> str:
    """チャンネルの Slack での名前（番号つき）から、時間の領域。測らないチャンネルなら空文字。"""
    return next((domain for prefix, domain in prefixes.items() if channel_name.startswith(prefix)), "")


def project_label(domain: str, label: str) -> str:
    """Toggl のプロジェクト名と記録の説明（研究 / vlm など）。"""
    return f"{DOMAINS[domain]} / {label.strip()}"


@dataclass(frozen=True)
class Entry:
    id: str
    user_id: str
    domain: str
    channel: str
    # テーマの名前（チャンネル名から番号を外したもの）
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
    """計測の開始・停止・メモ・送り先の状態と、カードの投稿、チャンネルの科目。records は core.records。"""

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

    def running_in(self, channel: str) -> Entry | None:
        """そのチャンネルで動いている計測（カードを描き直すときに使う）。"""
        running = (self.entry(str(item.get("entry") or "")) for item in self.records.items("active"))
        return next((entry for entry in running if entry is not None and entry.channel == channel), None)

    def start(self, user_id: str, domain: str, channel: str, theme: str,
              started_at: float | None = None, label: str = "") -> tuple[Entry, Entry | None]:
        """計測を始める。前の計測が動いていれば、その時刻で止める（止めた記録も返す）。

        そのチャンネルで科目を選んでいれば、大学の記録はその科目の名前で残す。label を渡せば、それを名前にする。
        """
        if not user_id or not channel or not theme or domain not in DOMAINS:
            raise ValueError("利用者・チャンネル・領域を指定してください")
        at = time.time() if started_at is None else started_at
        previous = self.stop(user_id, ended_at=at)
        page_id, course_name = self.course(channel)
        label = label.strip() or (course_name if domain == "course" and course_name else theme)
        entry = self._save(Entry(uuid.uuid4().hex, user_id, domain, channel, theme, page_id, course_name,
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
        return self._save(replace(entry, memo=memo.strip()[:1000]))

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

    def card(self, channel: str) -> str:
        """そのチャンネルに固定した時間記録カードの投稿の ts（無ければ空文字）。"""
        return str((self.records.get("card", channel) or {}).get("ts") or "")

    def cards(self) -> list[dict]:
        """カードの投稿（{"channel", "ts", "buttons"}）の全部。"""
        return self.records.items("card")

    def set_card(self, channel: str, ts: str, buttons: str) -> None:
        """カードの投稿を覚える。buttons は描いたときのボタンの名前の頭（変わったら描き直す）。"""
        self.records.put("card", channel, {"channel": channel, "ts": ts, "buttons": buttons})

    def forget_card(self, channel: str) -> None:
        self.records.delete("card", channel)

    def bind_course(self, channel: str, page_id: str, name: str) -> None:
        if not channel or not page_id or not name.strip():
            raise ValueError("チャンネルと科目を指定してください")
        self.records.put("course", channel, {"page_id": page_id, "name": name.strip()})

    def course(self, channel: str) -> tuple[str, str]:
        """そのチャンネルで選んだ科目（ページの id, 名前）。選んでいなければ空文字の組。"""
        found = self.records.get("course", channel) or {}
        return str(found.get("page_id") or ""), str(found.get("name") or "")
