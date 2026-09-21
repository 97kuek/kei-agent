"""Daily と振り返りの材料（digest）を作る。Claude はこれと、ここに書いたファイルを読んで書く。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from kei_agent import course, themes, timelog, work
from kei_agent.assistant import Assistant
from kei_agent.config import Config
from kei_agent.notion import NotionError
from kei_agent.slack_text import format_duration
from kei_agent.store import Store
from kei_agent.themes import OVERVIEW_DIR

# 材料に入れるノートの本文の長さ
NOTE_EXCERPT = 1500
# 大学・仕事の材料で、1行に並べる件数の上限（材料が長いと、要点が埋もれる）
MAX_DOMAIN_ITEMS = 4
LITERATURE_STATUS = {
    "posted": "新着あり（テーマのチャンネルに投稿済み）",
    "no_new": "新着なし",
    "no_keywords": "CLAUDE.md に検索キーワードがない",
    "no_channel": "チャンネルが見つからない",
    "error": "エラー",
}


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%m/%d %H:%M") if value else "-"


def _weekday(at: datetime) -> str:
    return "月火水木金土日"[at.weekday()]


def _at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _due_day(item: dict):
    at = _at(item.get("at", ""))
    return at.date() if at else None


def _dues(items: list[dict], with_day: bool = False) -> str:
    """締切を短い1行にまとめる（材料なので、素の文字のまま。Slack に出すのは Claude）。"""
    found = []
    for item in items[:MAX_DOMAIN_ITEMS]:
        at = _at(item.get("at", ""))
        head = f"{at.month}/{at.day} " if with_day and at else ""
        course = f"{item['course']} / " if item.get("course") else ""
        found.append(f"{head}{course}{item.get('title', '')}（{at:%H:%M}）" if at else str(item.get("title", "")))
    rest = f"（ほか {len(items) - MAX_DOMAIN_ITEMS} 件）" if len(items) > MAX_DOMAIN_ITEMS else ""
    return "、".join(found) + rest


def _events(items: list[dict], day) -> str:
    """その日の会議を短い1行に。件名と時刻だけで、本文と参加リンクは持ち出さない。"""
    found = []
    for item in items:
        at = _at(item.get("start", ""))
        if not at or at.date() != day:
            continue
        end = _at(item.get("end", ""))
        span = f"{at:%H:%M}–{end:%H:%M}" if end else f"{at:%H:%M}"
        found.append(f"{span} {item.get('subject', '')}")
    rest = f"（ほか {len(found) - MAX_DOMAIN_ITEMS} 件）" if len(found) > MAX_DOMAIN_ITEMS else ""
    return "、".join(found[:MAX_DOMAIN_ITEMS]) + rest


class DigestBuilder:
    def __init__(self, config: Config, store: Store, assistant: Assistant):
        self.config = config
        self.store = store
        self.assistant = assistant

    async def build(self, since: float, now: float, title: str, active_channels: set[str],
                    domains: bool = False) -> str:
        """active_channels は Kei Agent が参加しているチャンネル名。アーカイブしたテーマは材料に入れない。

        `domains` を立てると、大学と仕事の材料も集める。ここは相手のエージェントに聞きに行き、
        仕事の予定は claude を1回動かすので、朝（すでに朝のまとめで聞いている）では立てない。
        """
        lines = [f"# {title}", "", f"対象: {_ts(since)} 〜 {_ts(now)}", ""]
        lines += self._threads(since)
        lines += self._jobs(since)
        lines += self._night(since)
        lines += self._literature(since)
        lines += self._stalled(now, active_channels)
        lines += self._waiting(active_channels)
        lines += self._time(now)
        lines += self._yesterday_review(now)
        if domains:
            lines += await self._course(now)
            lines += await self._work(now)
        lines += ["", *await self._notion(since, now)]
        return "\n".join(lines) + "\n"

    async def _course(self, now: float) -> list[str]:
        """大学（今日が期限だったもの、残っている締切、明日の授業）。"""
        if course.AGENT not in self.assistant.agents:
            return []
        at = datetime.fromtimestamp(now)
        tomorrow = at + timedelta(days=1)
        lines = ["", "## 大学", ""]
        items = await self.assistant.course_due(course.DIGEST_DAYS, at)
        if items is None:
            return [*lines, "- 大学エージェントにつながらなかった", ""]
        today = [i for i in items if _due_day(i) == at.date()]
        rest = [i for i in items if _due_day(i) and _due_day(i) > at.date()]
        lines.append(f"- 今日が期限だったもの: {_dues(today) or 'なし'}")
        lines.append(f"- 残っている締切: {_dues(rest, with_day=True) or 'なし'}")
        classes = await self.assistant.ask_course(course.LIST_CLASSES, weekday=_weekday(tomorrow))
        names = [str(c.get("subject") or "") for c in (classes.data.get("items") or [])] if classes.ok else []
        lines.append(f"- 明日（{_weekday(tomorrow)}）の授業: {'、'.join(names) or 'なし'}")
        return [*lines, ""]

    async def _work(self, now: float) -> list[str]:
        """仕事（今日あった会議、明日の会議）。本文は持ち出さない（件名と時刻だけ）。"""
        if work.AGENT not in self.assistant.agents:
            return []
        at = datetime.fromtimestamp(now)
        lines = ["", "## 仕事", ""]
        reply = await self.assistant.ask_work(work.LIST_EVENTS, days=2)
        if not reply.ok:
            return [*lines, "- 仕事エージェントにつながらなかった", ""]
        events = reply.data.get("items") or []
        lines.append(f"- 今日あった会議: {_events(events, at.date()) or 'なし'}")
        lines.append(f"- 明日の会議: {_events(events, (at + timedelta(days=1)).date()) or 'なし'}")
        return [*lines, ""]

    def _threads(self, since: float) -> list[str]:
        lines = ["## やり取りのあったスレッド", ""]
        found = False
        for r in self.store.threads_updated_since(since):
            try:
                ws = themes.resolve(self.config, r["channel_name"])
            except ValueError:
                continue
            log_path = ws.cwd / ".kei-agent" / "threads" / f"{r['thread_ts']}.md" if ws.cwd else None
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
        for d in themes.theme_dirs(self.config):
            channel = d.name
            if channel not in active_channels:
                continue
            # 一度も依頼のなかったテーマは、招待しただけなので停滞として扱わない
            last = activity.get(channel)
            if last is not None and last < limit:
                lines.append(f"- #{channel}（最後のやり取り {_ts(last)}）")
        return lines if len(lines) > 3 else lines + ["- なし"]

    def _waiting(self, active_channels: set[str]) -> list[str]:
        lines = ["", "## 返事待ちのスレッド", ""]
        rows = [r for r in self.store.threads_awaiting() if r["channel_name"] in active_channels]
        lines += [f"- #{r['channel_name']}（{_ts(r['awaiting_since'])} から）" for r in rows]
        return lines if rows else lines + ["- なし"]

    def _time(self, now: float) -> list[str]:
        """研究時間（人は Toggl、Kei Agent は runs）。材料の CSV も書き出す。"""
        lines = ["", "## 研究時間（今週）", ""]
        try:
            path = timelog.write_week(self.config, self.store, timelog.load_toggl(),
                                      datetime.fromtimestamp(now).date())
            return lines + timelog.week_summary(path)
        except OSError as e:
            return lines + [f"- 材料を書けなかった: {e}"]

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
