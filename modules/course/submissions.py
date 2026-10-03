"""Moodle が確認した提出・受験終了を、既存の Notion 課題へ反映する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from kei_agent_a2a.api import NotionError

from . import moodle_api, notion_sync
from .notion_props import plain

# 1回の見回りを A2A の待ち時間内に収める。次回は記録したページ ID の続きから読む。
MAX_CHECKS = 10


@dataclass
class Result:
    enabled: bool = True
    completed: list[str] = field(default_factory=list)
    checked: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    next_id: str = ""

    def summary(self) -> str:
        if not self.enabled:
            return "提出状態の同期は未設定です。MOODLE_API_URL と MOODLE_API_TOKEN を設定してください"
        lines = [f"Moodle の提出・受験状態を確認しました（確認 {self.checked} 件・提出済みへの更新 {len(self.completed)} 件）"]
        lines.extend(f"• 提出済み: {title}" for title in self.completed)
        if self.skipped:
            lines.append(f"活動を特定できない {self.skipped} 件は変更しませんでした")
        if self.errors:
            lines.append(f"確認・反映に失敗した {len(self.errors)} 件は変更しませんでした")
            lines.append("理由: " + self.errors[0])
        if self.next_id:
            lines.append("残りは次回の見回りで確認します")
        return "\n".join(lines)


def sync(*, api: moodle_api.Client | None = None, client: notion_sync.CourseNotion | None = None,
         after: str = "") -> Result:
    api = api or moodle_api.from_env()
    if api is None:
        return Result(enabled=False)
    client = client or notion_sync._client()
    rows = sorted(client.taken().values(), key=lambda row: row["id"])
    pending = [row for row in rows if ((row.get("properties", {}).get("状態") or {}).get("status") or {}).get("name")
               != "提出済み"]
    # 最後まで読んだ次の回は先頭へ戻る。完了した行が消えてもカーソルは有効。
    remaining = [row for row in pending if row["id"] > after] or pending
    batch = remaining[:MAX_CHECKS]
    result = Result(next_id=batch[-1]["id"] if len(remaining) > MAX_CHECKS else "")
    cache = {}
    updates = []
    for row in batch:
        props = row["properties"]
        title = notion_sync.notice_title(plain(props.get("課題")))
        url = (props.get("Moodle") or {}).get("url") or ""
        uid = plain(props.get("Moodle ID"))
        try:
            key = url or uid
            if key not in cache:
                cache[key] = api.completion(url, uid)
            outcome = cache[key]
        except moodle_api.APIError as error:
            result.errors.append(str(error))
            continue
        if outcome is None:
            result.skipped += 1
            continue
        result.checked += 1
        if outcome.completed:
            changes = {"状態": {"status": {"name": "提出済み"}}}
            if not url:
                changes["Moodle"] = {"url": outcome.url}
            updates.append((row["id"], title, changes))
    for page_id, title, changes in updates:
        try:
            client.notion.request("PATCH", f"/pages/{page_id}", {"properties": changes})
        except NotionError:
            # 先に成功した行を結果に残し、カレンダーへの再反映と続きの確認を止めない。
            result.errors.append("Notion の課題の状態を更新できませんでした")
            continue
        result.completed.append(title)
    return result


def record_result(result: Result, records) -> None:
    records.put("submissions", "cursor", {"after": result.next_id})
    if result.completed:
        records.put("calendar", "dirty", {"dirty": True, "version": uuid4().hex})


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    from kei_agent_a2a.api import load_config, records

    argparse.ArgumentParser(prog="kei-agent-module course sync-submissions",
                            description="Moodle の提出・受験終了を Notion に同期する").parse_args(argv)
    checkpoint = records(load_config(), "course")
    try:
        result = sync(after=(checkpoint.get("submissions", "cursor") or {}).get("after", ""))
        record_result(result, checkpoint)
        print(result.summary())
        if not result.enabled or result.errors:
            sys.exit(1)
    except (moodle_api.APIError, notion_sync.SyncError, NotionError) as error:
        sys.exit(str(error))
