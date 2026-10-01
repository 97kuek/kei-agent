"""Daily と Retro & Planning の材料（digest）を作る。材料はファイルにせず、そのままプロンプトに入れる。

モデルはこれと、ここに書いたスレッドのログを読んで書く。前日の振り返りは共通 Notion ホームから読む。
モジュールの材料（時間記録のモジュールなら今週の人の時間）は、class Module の material が足す。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from kei_agent import deadline, timelog
from kei_agent.configuration.config import Config
from kei_agent.dates import parse_time
from kei_agent.framework import modules
from kei_agent.slack_text import format_duration
from kei_agent.storage.notion import NotionError
from kei_agent.storage.store import Store
from kei_agent.workspaces import themes

if TYPE_CHECKING:
    from kei_agent.assistant import Assistant

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


def _due_day(item: dict):
    at = parse_time(item.get("at", ""))
    return deadline.day(at) if at else None


def _dues(items: list[dict], with_day: bool = False) -> str:
    """締切を短い1行にまとめる（材料なので、素の文字のまま。Slack に出すのは Claude）。"""
    found = []
    for item in items[:MAX_DOMAIN_ITEMS]:
        at = parse_time(item.get("at", ""))
        head = f"{deadline.day(at).month}/{deadline.day(at).day} " if with_day and at else ""
        course = f"{item['course']} / " if item.get("course") else ""
        found.append(f"{head}{course}{item.get('title', '')}（{deadline.clock(at)}）" if at else str(item.get("title", "")))
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


# 振り返りの材料に、モジュールの予定をどこまで入れるか（日。締切は、その先まで出るものを全部）
DIGEST_AGENDA_DAYS = 7


def _events(items: list[dict], day) -> str:
    """その日の会議を短い1行に（材料なので、件名と時刻だけで足りる）。"""
    found = []
    for item in items:
        at = parse_time(item.get("start", ""))
        if not at or at.date() != day:
            continue
        end = parse_time(item.get("end", ""))
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
                    domains: bool = False, skip: str = "") -> str:
        """active_channels は Kei Agent が参加しているチャンネル名。アーカイブしたテーマは材料に入れない。

        `domains` を立てると、大学とモジュールの予定（仕事の会議など）の材料も集める。ここは相手のエージェントに
        聞きに行き、仕事の予定は claude を1回動かすので、朝（すでに朝のまとめで聞いている）では立てない。
        skip は材料を足さないモジュール（材料を集めている、Daily のモジュール自身）。
        """
        lines = [f"# {title}", "", f"対象: {_ts(since)} 〜 {_ts(now)}", ""]
        lines += self._threads(since)
        lines += self._jobs(since)
        lines += self._night(since)
        lines += self._stalled(now, active_channels)
        lines += self._waiting(active_channels)
        lines += await self.assistant.module_material(now, skip)
        lines += self._agent_time(now)
        if domains:
            lines += await self._agenda(now)
        # 長くなりうる本文（前日の振り返り、ノート）は最後に置く。上限を超えたらそこから削れる
        tasks, notes = await self._notion(since, now)
        lines += ["", *tasks]
        lines += await self._yesterday_review(now)
        lines += ["", *notes]
        return cap("\n".join(lines) + "\n")

    async def _agenda(self, now: float) -> list[str]:
        """モジュールの予定（今日あったもの、明日のもの、締切）。材料なので、件名と時刻だけ。"""
        at = datetime.fromtimestamp(now)
        tomorrow = (at + timedelta(days=1)).date()
        agenda, unread = await self.assistant.module_agenda(DIGEST_AGENDA_DAYS)
        lines: list[str] = []
        for name, items in agenda.items():
            timed = [item for item in items if item.get("kind", "meeting") in ("meeting", "class")]
            dues = [item for item in items if item.get("kind") == "due"]
            lines += ["", f"## {modules.known()[name].label}", ""]
            if timed or not dues:
                lines += [f"- 今日あった予定: {_events(timed, at.date()) or 'なし'}",
                          f"- 明日の予定: {_events(timed, tomorrow) or 'なし'}"]
            if dues:
                lines += [f"- 今日が期限だったもの: {_dues([i for i in dues if _due_day(i) == at.date()]) or 'なし'}",
                          "- 残っている締切: "
                          + (_dues([i for i in dues if _due_day(i) and _due_day(i) > at.date()], with_day=True) or "なし")]
            lines.append("")
        for label in unread:
            lines += ["", f"## {label}", "", "- 予定を読めなかった", ""]
        return lines

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
        for channel in themes.all_themes(self.config):
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

    def _agent_time(self, now: float) -> list[str]:
        """今週の Kei Agent の稼働（runs から数える）。人の時間は、時間記録のモジュールが材料に足す。"""
        monday = timelog.week_start(datetime.fromtimestamp(now).date())
        since = datetime.combine(monday, datetime.min.time()).timestamp()
        agent = sum(timelog.assistant_seconds(self.store, since, now).values())
        return ["", "## Kei Agent の稼働（今週）", "", f"- 合計 {agent / 3600:.1f} 時間"]

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
