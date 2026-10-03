"""MCP で時間を測り、Toggl と共通ホームへ送る。直接計測の取り込みと振り返りの材料も受け持つ。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from kei_agent.api import Core, NotionError, TogglAmbiguousWrite, TogglError, load_toggl, theme_name

from . import importer
from .entries import DOMAINS, TOGGL_SENT, Entries, Entry

log = logging.getLogger(__name__)


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.entries = Entries(core.records)
        self._retrying = False
        # 確認・メモと見回りの送信を直列にし、待っていた処理も最新の送信状態を読む
        self._delivery_lock = asyncio.Lock()

    async def head_action(self, name: str, params: dict) -> dict | None:
        if name != "timer":
            return None
        action = str(params.get("action") or "")
        user_id = self.core.owner_id
        stopped = None
        if action == "start":
            domain, label = str(params.get("domain") or ""), str(params.get("label") or "").strip()
            if domain not in DOMAINS or not label:
                raise ValueError(f"始めるときは domain（{' / '.join(DOMAINS)}）と label（テーマや科目の名前）を渡してください")
            channel = await self._head_channel(domain, label)
            _, stopped = self.entries.start(user_id, domain, channel, theme_name(label), label=label)
        elif action == "stop":
            stopped = self.entries.stop(user_id)
        elif action == "memo":
            memo = str(params.get("memo") or "").strip()
            if not memo:
                raise ValueError("追記する memo を渡してください")
            async with self._delivery_lock:
                entry_id = str(params.get("entry_id") or "")
                entry = self._owned_entry(entry_id) if entry_id else self.entries.active(user_id)
                if entry is None:
                    raise ValueError("計測中の時間記録がありません。entry_id を指定してください")
                entry = self.entries.add_memo(entry.id, memo)
                if entry.ended_at is not None:
                    await self._sync_locked(entry)
        elif action == "resolve":
            entry_id, resolution = str(params.get("entry_id") or ""), str(params.get("resolution") or "")
            if not entry_id or resolution not in ("recorded", "missing"):
                raise ValueError("entry_id と resolution（recorded / missing）を渡してください")
            async with self._delivery_lock:
                entry = self._owned_entry(entry_id)
                if entry.ended_at is None or entry.toggl_state != "needs_review":
                    raise ValueError("確認できるのは停止済みで Toggl の確認が必要な記録だけです")
                entry = self.entries.set_delivery(entry.id, toggl_state="done" if resolution == "recorded" else "pending")
                await self._sync_locked(entry)
        elif action != "status":
            raise ValueError("action は start / stop / status / memo / resolve のどれかにしてください")
        if stopped is not None:
            await self._sync(stopped)
        return {"running": _shown(self.entries.active(user_id)),
                "stopped": _shown(self.entries.entry(stopped.id)) if stopped else None,
                "needs_review": [_shown(entry) for entry in self.entries.finished()
                                 if entry.user_id == user_id and entry.toggl_state == "needs_review"]}

    def _owned_entry(self, entry_id: str) -> Entry:
        entry = self.entries.entry(entry_id)
        if entry is None or entry.user_id != self.core.owner_id:
            raise ValueError("自分の時間記録が見つかりません")
        return entry

    async def _head_channel(self, domain: str, label: str) -> str:
        """確認の知らせの届け先。研究はテーマの、大学・仕事は担当のチャンネル。"""
        names = self.core.channels(domain)
        name = theme_name(label) if domain == "research" or not names else names[0]
        return (await self.core.channel_ids()).get(name, name)

    async def _sync(self, entry: Entry) -> None:
        async with self._delivery_lock:
            current = self.entries.entry(entry.id)
            if current is not None:
                await self._sync_locked(current)

    async def _sync_locked(self, entry: Entry) -> None:
        """Toggl が曖昧なら利用者の確認を待ち、それ以外の保留だけを送り直す。"""
        if entry.ended_at is None or entry.toggl_state == "needs_review":
            return
        if entry.toggl_state not in TOGGL_SENT:
            toggl = load_toggl()
            if toggl is None:
                entry = self.entries.set_delivery(entry.id, toggl_state="not_configured")
            else:
                # 送信中の中断や終了でも、再起動後に勝手に再送しない
                self.entries.set_delivery(entry.id, toggl_state="needs_review")
                try:
                    await self.core.to_thread(toggl.record_completed, entry.description, entry.description,
                                              datetime.fromtimestamp(entry.started_at).astimezone(),
                                              max(1, int(entry.ended_at - entry.started_at)))
                except TogglAmbiguousWrite:
                    await self.core.post(entry.channel,
                                         f"Toggl の確認が必要です: {entry.description}（{entry.minutes}分、記録 ID: {entry.id}）。"
                                         "Toggl にあるか確認し、timer の resolve で recorded（ある）か missing（ない）を指定してください。")
                    return
                except TogglError as error:
                    log.warning("Toggl に送れないので後で再送します: %s", error)
                    self.entries.set_delivery(entry.id, toggl_state="pending")
                    return
                entry = self.entries.set_delivery(entry.id, toggl_state="done")
        if entry.notion_state == "done":
            return
        hub = self.core.hub
        if hub is None or not hub.has_time_db:
            return
        started_at = datetime.fromtimestamp(entry.started_at).astimezone().isoformat()
        try:
            await self.core.to_thread(hub.record_time, entry.id, entry.domain, entry.label, started_at,
                                      entry.minutes, entry.memo, "", "Slack")
        except Exception:
            log.warning("Notion の時間記録は後で再試行します", exc_info=True)
            self.entries.set_delivery(entry.id, notion_state="pending")
        else:
            self.entries.set_delivery(entry.id, notion_state="done")

    async def tick(self, now: datetime) -> None:
        """毎分。保留中の記録の再送を、定期処理を待たせずに1本ずつ行う。"""
        if not self._retrying:
            self._retrying = True
            self.core.spawn(self._look_around())

    async def _look_around(self) -> None:
        try:
            for entry in self.entries.pending():
                if entry.toggl_state != "needs_review":
                    await self._sync(entry)
        finally:
            self._retrying = False

    async def run_schedule(self, name: str, day: str) -> dict:
        """Toggl のアプリで直接測った記録を、共通ホームの「時間記録」に取り込む。"""
        hub = self.core.hub
        if hub is None or not hub.has_time_db:
            return {"status": "skipped", "reason": "no_hub"}
        toggl = load_toggl()
        if toggl is None:
            return {"status": "skipped", "reason": "no_toggl"}
        until = date.fromisoformat(day)
        since = until - timedelta(days=importer.IMPORT_DAYS - 1)
        # Slack で測った分（Toggl にも送ってある）。記録は別スレッドから触れないので、先に読む
        start = datetime.combine(since, datetime.min.time()).timestamp() - 86400
        own = [(e.started_at, e.ended_at - e.started_at) for e in self.entries.finished(start)]
        try:
            return await self.core.to_thread(importer.import_toggl, toggl, hub, own, since, until)
        except (TogglError, NotionError) as e:
            log.warning("Toggl の記録を時間記録に取り込めませんでした: %s", e)
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    async def material(self, now: float) -> list[str]:
        """今週の人の時間（共通ホームの「時間記録」から）。Kei Agent の稼働は本体が数える。"""
        today = datetime.fromtimestamp(now).date()
        monday = today - timedelta(days=today.weekday())
        lines = ["", "## 時間（今週）", ""]
        hub = self.core.hub
        if hub is None:
            return [*lines, "- 人: 共通 Notion ホームが使えないので分からない"]
        try:
            minutes = await self.core.to_thread(hub.time_minutes_by_domain, monday)
        except NotionError as e:
            lines.append(f"- 人: 時間記録を読めなかった（{e}）")
        else:
            order = [*DOMAINS.values(), *sorted(set(minutes) - set(DOMAINS.values()))]
            parts = "、".join(f"{d} {minutes[d] / 60:.1f} 時間" for d in order if minutes.get(d))
            total = sum(minutes.values())
            marks = "/`・`".join(DOMAINS.values())
            lines.append(f"- 人: 合計 {total / 60:.1f} 時間（{parts}）" if total else
                         "- 人: 今週はまだ記録がない（Dot に頼んで MCP の timer で測る。"
                         f"Toggl で直接測るならプロジェクト名の先頭に `{marks}/`）")
        if url := hub.time_url():
            lines.append(f"- 時間記録（週ごとのグラフ）: {url}")
        return lines


def _shown(entry: Entry | None) -> dict | None:
    """Dot に記録 ID・説明・時刻・時間・メモ・送信状態を返す。"""
    if entry is None:
        return None
    return {"id": entry.id, "description": entry.description,
            "started": datetime.fromtimestamp(entry.started_at).strftime("%H:%M"),
            "minutes": entry.minutes if entry.ended_at is not None else int((time.time() - entry.started_at) // 60),
            "memo": entry.memo, "toggl_state": entry.toggl_state, "notion_state": entry.notion_state}
