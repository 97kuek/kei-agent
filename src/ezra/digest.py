"""Daily と振り返りの材料（digest）を作る。Claude はこれと、ここに書いたファイルを読んで書く。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ezra import themes
from ezra.assistant import Assistant, format_duration
from ezra.config import Config
from ezra.notion import NotionError
from ezra.store import Store
from ezra.themes import OVERVIEW_DIR

# 材料に入れるノートの本文の長さ
NOTE_EXCERPT = 1500
LITERATURE_STATUS = {
    "posted": "新着あり（テーマのチャンネルに投稿済み）",
    "no_new": "新着なし",
    "no_keywords": "CLAUDE.md に検索キーワードがない",
    "no_channel": "チャンネルが見つからない",
    "error": "エラー",
}


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%m/%d %H:%M") if value else "-"


def theme_dirs(research_root: Path) -> list[Path]:
    """CLAUDE.md のあるテーマのディレクトリ。_overview や _ezra_state などは含めない。"""
    if not research_root.is_dir():
        return []
    return sorted(p for p in research_root.iterdir()
                  if p.is_dir() and not p.name.startswith((".", "_")) and (p / "CLAUDE.md").exists())


class DigestBuilder:
    def __init__(self, config: Config, store: Store, assistant: Assistant):
        self.config = config
        self.store = store
        self.assistant = assistant

    async def build(self, since: float, now: float, title: str, active_channels: set[str]) -> str:
        """active_channels は Ezra が参加しているチャンネル名。アーカイブしたテーマは材料に入れない。"""
        lines = [f"# {title}", "", f"対象: {_ts(since)} 〜 {_ts(now)}", ""]
        lines += self._threads(since)
        lines += self._jobs(since)
        lines += self._night(since)
        lines += self._literature(since)
        lines += self._stalled(now, active_channels)
        lines += self._waiting(active_channels)
        lines += self._yesterday_review(now)
        lines += ["", *await self._notion(since, now)]
        return "\n".join(lines) + "\n"

    def _threads(self, since: float) -> list[str]:
        lines = ["## やり取りのあったスレッド", ""]
        found = False
        for r in self.store.threads_updated_since(since):
            try:
                ws = themes.resolve(self.config, r["channel_name"])
            except ValueError:
                continue
            log_path = ws.cwd / ".ezra" / "threads" / f"{r['thread_ts']}.md" if ws.cwd else None
            if log_path and log_path.exists():
                lines.append(f"- #{r['channel_name']}（最後 {_ts(r['updated_at'])}）: `{log_path}`")
                found = True
        return lines if found else lines + ["- なし"]

    def _jobs(self, since: float) -> list[str]:
        lines = ["", "## 終わったジョブ", ""]
        jobs = self.store.jobs_finished_since(since)
        for j in jobs:
            took = format_duration(j.finished_at - j.started_at) if j.started_at and j.finished_at else "-"
            lines.append(f"- `{j.cwd}` ジョブ{j.id}「{j.name}」: {j.status}（{took}）{j.detail or ''}")
        return lines if jobs else lines + ["- なし"]

    def _night(self, since: float) -> list[str]:
        lines = ["", "## 夜間の Task", ""]
        last = self.store.last_schedule("night")
        night = json.loads(last["detail"] or "{}") if last and last["ran_at"] >= since else {}
        for t in night.get("tasks") or []:
            lines.append(f"- {t.get('title')}（{t.get('theme') or '-'}）: {t.get('status')} "
                         f"{t.get('summary') or t.get('reason') or ''} {t.get('url') or ''}".rstrip())
        if not night.get("tasks"):
            lines.append("- なし" if night.get("status") != "error" else f"- Notion から読めなかった: {night.get('error')}")
        return lines

    def _literature(self, since: float) -> list[str]:
        lines = ["", "## 先行研究の新着", ""]
        last = self.store.last_schedule("literature")
        if not (last and last["ran_at"] >= since):
            return lines + ["- この期間は確認していない"]
        for name, info in (json.loads(last["detail"] or "{}").get("themes") or {}).items():
            lines.append(f"- {name}: {LITERATURE_STATUS.get(info.get('status'), info.get('status'))}")
        return lines

    def _stalled(self, now: float, active_channels: set[str]) -> list[str]:
        days = self.config.schedule.stall_days
        lines = ["", f"## {days}日以上やり取りのないテーマ", ""]
        activity = self.store.last_activity_by_channel_name()
        limit = now - days * 86400
        for d in theme_dirs(self.config.research_root):
            channel = d.name
            if channel not in active_channels:
                continue
            last = activity.get(channel)
            if (last or d.stat().st_mtime) < limit:
                lines.append(f"- #{channel}（最後のやり取り {_ts(last)}）")
        return lines if len(lines) > 3 else lines + ["- なし"]

    def _waiting(self, active_channels: set[str]) -> list[str]:
        lines = ["", "## 返事待ちのスレッド", ""]
        rows = [r for r in self.store.threads_awaiting() if r["channel_name"] in active_channels]
        lines += [f"- #{r['channel_name']}（{_ts(r['awaiting_since'])} から）" for r in rows]
        return lines if rows else lines + ["- なし"]

    def _yesterday_review(self, now: float) -> list[str]:
        yesterday = (datetime.fromtimestamp(now).date() - timedelta(days=1)).isoformat()
        review = self.config.research_root / OVERVIEW_DIR / "reviews" / f"{yesterday}.md"
        return ["", "## 前日の振り返り（ファイル）", "", f"- `{review}`" if review.exists() else "- なし"]

    async def _notion(self, since: float, now: float) -> list[str]:
        notion = self.assistant.notion
        if notion is None:
            return ["## Notion", "", "- 設定されていない"]
        today = datetime.fromtimestamp(now).date()
        try:
            notes = await asyncio.to_thread(notion.notes_edited_since, datetime.fromtimestamp(since),
                                            ["計画", "考察", "振り返り"])
            awaiting = await asyncio.to_thread(notion.awaiting_tasks)
            due = await asyncio.to_thread(notion.tasks_due_within, today, 3)
            milestones = await asyncio.to_thread(notion.upcoming_milestones, today)
            tonight = await asyncio.to_thread(notion.count_tonight_tasks)
        except NotionError as e:
            await self.assistant.notify_trouble(f"Daily の材料を Notion から読めませんでした: {e}")
            return ["## Notion", "", f"- 読めなかった: {e}"]

        lines = ["## Notion: 前回以降に書かれた計画・考察・振り返りのノート", ""]
        for n in notes:
            body = n.body if len(n.body) <= NOTE_EXCERPT else n.body[:NOTE_EXCERPT] + "…（続きは Notion）"
            lines += [f"### {n.kind}: {n.title}（{n.day or '-'}） {n.url}", "", body or "（本文なし）", ""]
        if not notes:
            lines += ["- なし", ""]
        lines += ["## Notion: 確認待ちの Task", ""]
        lines += [f"- {t.title}（{', '.join(t.theme_names) or '-'}） {t.url}" for t in awaiting] or ["- なし"]
        lines += ["", "## Notion: 期日が3日以内の Task", ""]
        lines += [f"- {t.due} {t.title}（{t.status}・{t.assignee or '-'}） {t.url}" for t in due] or ["- なし"]
        lines += ["", "## Notion: 近いマイルストーン", ""]
        lines += [f"- {m['due']} {m['name']} {m['url']}" for m in milestones] or ["- なし"]
        lines += ["", f"- 今夜やる Task: {tonight} 件"]
        return lines
