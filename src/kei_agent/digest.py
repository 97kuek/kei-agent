"""Daily と Retro & Planning の材料（digest）を作る。材料はファイルにせず、そのままプロンプトに入れる。

モデルはこれと、ここに書いたスレッドのログを読んで書く。前日の振り返りと今週の時間は共通 Notion ホームから読む。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from kei_agent import course, morning, themes, timelog, work
from kei_agent.assistant import Assistant
from kei_agent.config import Config
from kei_agent.notion import NotionError
from kei_agent.slack_text import format_duration
from kei_agent.store import Store

# 材料に入れるノートの本文の長さ
NOTE_EXCERPT = 1500
# 前日の振り返り（貼られた結論を含む）を材料に入れる長さ
REVIEW_EXCERPT = 4000
# 材料全体の上限（字）。プロンプトに直接入れるので、長い本文を後ろに置き、超えたら後ろから削る
MAX_DIGEST_CHARS = 30000
TRUNCATED = "（材料が長いので、ここから先は省いた）"
# 大学・仕事の材料で、1行に並べる件数の上限（材料が長いと、要点が埋もれる）
MAX_DOMAIN_ITEMS = 4
# Notion の Task の「終わった」状態の名前
DONE = "完了"


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%m/%d %H:%M") if value else "-"


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


def _excerpt(text: str, limit: int, rest: str = "…（続きは Notion）") -> str:
    return text if len(text) <= limit else text[:limit] + rest


def cap(text: str, limit: int = MAX_DIGEST_CHARS) -> str:
    """長すぎる材料を、行の切れ目で limit 字までにする。削ったことは最後の1行で分かるようにする。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    head = head[:head.rfind("\n") + 1] or head
    return f"{head}\n{TRUNCATED}\n"


def _events(items: list[dict], day) -> str:
    """その日の会議を短い1行に（材料なので、件名と時刻だけで足りる）。"""
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
        lines += self._stalled(now, active_channels)
        lines += self._waiting(active_channels)
        lines += await self._time(now)
        if domains:
            lines += await self._course(now)
            lines += await self._work(now)
        # 長くなりうる本文（前日の振り返り、ノート）は最後に置く。上限を超えたらそこから削れる
        tasks, notes = await self._notion(since, now)
        lines += ["", *tasks]
        lines += await self._yesterday_review(now)
        lines += ["", *notes]
        return cap("\n".join(lines) + "\n")

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
        classes = await self.assistant.ask_course(course.LIST_CLASSES, weekday=morning.weekday(tomorrow))
        names = [str(c.get("subject") or "") for c in (classes.data.get("items") or [])] if classes.ok else []
        lines.append(f"- 明日（{morning.weekday(tomorrow)}）の授業: {'、'.join(names) or 'なし'}")
        return [*lines, ""]

    async def _work(self, now: float) -> list[str]:
        """仕事（今日あった会議、明日の会議）。材料なので、件名と時刻だけ。"""
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

    async def _time(self, now: float) -> list[str]:
        """今週の時間。人は共通ホームの「時間記録」から、Kei Agent の稼働は runs から数える。"""
        today = datetime.fromtimestamp(now).date()
        monday = timelog.week_start(today)
        lines = ["", "## 時間（今週）", ""]
        hub = self.assistant.hub
        if hub is None:
            lines.append("- 人: 共通 Notion ホームが使えないので分からない")
        else:
            try:
                minutes = await asyncio.to_thread(hub.time_minutes_by_domain, monday)
            except NotionError as e:
                lines.append(f"- 人: 時間記録を読めなかった（{e}）")
            else:
                order = [*timelog.DOMAINS, *sorted(set(minutes) - set(timelog.DOMAINS))]
                parts = "、".join(f"{d} {minutes[d] / 60:.1f} 時間" for d in order if minutes.get(d))
                total = sum(minutes.values())
                lines.append(f"- 人: 合計 {total / 60:.1f} 時間（{parts}）" if total else
                             "- 人: 今週はまだ記録がない（Slack の /toggl か時間記録カードで測る。"
                             f"Toggl で直接測るならプロジェクト名の先頭に `{'/`・`'.join(timelog.DOMAINS)}/`）")
        since = datetime.combine(monday, datetime.min.time()).timestamp()
        agent = sum(timelog.assistant_seconds(self.store, since, now).values())
        lines.append(f"- Kei Agent の稼働: {agent / 3600:.1f} 時間")
        if hub is not None and hub.time_url():
            lines.append(f"- 時間記録（週ごとのグラフ）: {hub.time_url()}")
        return lines

    async def _yesterday_review(self, now: float) -> list[str]:
        """前日のレトプラ（貼られた結論を含む）。共通ホームの日別記録から読む。"""
        yesterday = (datetime.fromtimestamp(now).date() - timedelta(days=1)).isoformat()
        lines = ["", f"## 前日の振り返り（{yesterday}）", ""]
        hub = self.assistant.hub
        if hub is None:
            return [*lines, "- 共通 Notion ホームが使えないので読めなかった"]
        try:
            text = await asyncio.to_thread(hub.review_text, yesterday)
        except NotionError as e:
            return [*lines, f"- 読めなかった: {e}"]
        return [*lines, _excerpt(text.strip(), REVIEW_EXCERPT) if text.strip() else "- なし"]

    async def _notion(self, since: float, now: float) -> tuple[list[str], list[str]]:
        """（Task とマイルストーンの一覧, ノートの本文）。一覧は短く、今日のタスクの元になるので先に置く。"""
        notion = self.assistant.notion
        if notion is None:
            return ["## Notion", "", "- 設定されていない"], []
        today = datetime.fromtimestamp(now).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        try:
            notes = await asyncio.to_thread(notion.notes_edited_since, datetime.fromtimestamp(since),
                                            ["計画", "考察", "振り返り"])
            if self.assistant.hub is not None:
                hub_reviews = await asyncio.to_thread(
                    self.assistant.hub.reviews_edited_since, datetime.fromtimestamp(since))
                # 移行中の旧レトプラは残すが、新 DB に同じ日の記録がある場合は二重に載せない。
                migrated_days = {review.day for review in hub_reviews}
                notes = [n for n in notes if n.kind != "振り返り" or n.day not in migrated_days]
                # 前日の分は「前日の振り返り」に丸ごと入れてある
                notes += [review for review in hub_reviews if review.day != yesterday]
            today_tasks = await asyncio.to_thread(notion.tasks_due_on, today)
            awaiting = await asyncio.to_thread(notion.awaiting_tasks)
            due = await asyncio.to_thread(notion.tasks_due_within, today, 3)
            milestones = await asyncio.to_thread(notion.upcoming_milestones, today)
            tonight = await asyncio.to_thread(notion.count_tonight_tasks)
        except NotionError as e:
            await self.assistant.notify_trouble(f"Daily の材料を Notion から読めませんでした: {e}")
            return ["## Notion", "", f"- 読めなかった: {e}"], []

        # Daily の「今日のタスク」になる。済みも入れて、やったことも見せる
        lines = ["## Notion: 今日が期日の Task（済みを含む）", ""]
        lines += [f"- {'済' if t.status == DONE else '未'} {t.title}（{t.status}） {t.url}"
                  for t in today_tasks] or ["- なし"]
        lines += ["", "## Notion: 確認待ちの Task", ""]
        lines += [f"- {t.title}（{', '.join(t.theme_names) or '-'}） {t.url}" for t in awaiting] or ["- なし"]
        lines += ["", "## Notion: 期日が3日以内の Task", ""]
        lines += [f"- {t.due} {t.title}（{t.status}・{t.assignee or '-'}） {t.url}" for t in due] or ["- なし"]
        lines += ["", "## Notion: 近いマイルストーン", ""]
        lines += [f"- {m['due']} {m['name']} {m['url']}" for m in milestones] or ["- なし"]
        lines += ["", f"- 今夜やる Task: {tonight} 件"]

        bodies = ["## Notion: 前回以降に書かれた計画・考察・振り返りのノート", ""]
        for n in notes:
            bodies += [f"### {n.kind}: {n.title}（{n.day or '-'}） {n.url}", "",
                       _excerpt(n.body, NOTE_EXCERPT) or "（本文なし）", ""]
        if not notes:
            bodies += ["- なし"]
        return lines, bodies
