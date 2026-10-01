"""決まった時刻の処理: 🌙 の夜間 Task、保守、放置されたスレッドへの声かけ。

モジュールの定期処理（module.toml の [schedules]）と、モジュールが受け持つ本体の定期処理（core_schedules。
Daily と Retro & Planning）も同じ順番の中で動かし、中身はモジュールの run_schedule に任せる。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import time
from contextlib import suppress
from datetime import date, datetime, timedelta
from datetime import time as dtime

import aiohttp

from kei_agent.configuration.config import Config
from kei_agent.conversation.assistant import Assistant
from kei_agent.conversation.request import Request
from kei_agent.conversation.slack_text import AWAITING_MARKER, clean_text, format_duration
from kei_agent.execution import jobs
from kei_agent.framework import modules, version
from kei_agent.scheduling import briefing, maintenance
from kei_agent.storage import settings
from kei_agent.storage.notion import NotionError
from kei_agent.storage.notion_store import Task, parse_slack_permalink, summarize
from kei_agent.storage.store import Store
from kei_agent.workspaces import themes

log = logging.getLogger(__name__)

# 実行する順番。夜間の Task の結果を Daily に載せるため、night を先にする
def task_names(config: Config) -> tuple[str, ...]:
    """実行する順。同じ時刻なら、夜間の Task → モジュールの処理（朝の読みものなど）→ Daily → 振り返り → 保守。

    夜間の Task とモジュールの処理の結果を、Daily と朝の一覧に載せるため。Daily と振り返りは、受け持つモジュール
    （core_schedules。Daily・振り返りのモジュール）があるときだけ動く。
    """
    taken = tuple(name for name in modules.CORE_SCHEDULES if modules.core_schedule_owner(config.modules, name))
    return ("night", *(s.name for s in settings.module_schedules(config)), *taken, "maintenance")
# 夜間の Task は、朝に Mac が起きたときにも実行する
NIGHT_CATCH_UP_HOURS = 12
# 取り込んだ新しい版で起動し直したかを見る間隔（秒）。見つけてから、もう一度この時間たっても古ければ知らせる
VERSION_CHECK_SECONDS = 3600
# 「一度だけ知らせた」目印を残す日数（学期の終わりまで持たなくてよい）
NOTICE_RETENTION_DAYS = 60
# 声のレイヤに渡す日数（briefing.py）
VOICE_DAYS = briefing.VOICE_DAYS


def due_day(now: datetime, hhmm: str, catch_up_hours: float) -> str | None:
    """now が、その日（または前日）の hhmm から catch_up_hours 以内なら、その日付を返す。"""
    if not hhmm:
        return None
    hour, minute = (int(x) for x in hhmm.split(":"))
    for offset in (0, -1):
        day = now.date() + timedelta(days=offset)
        at = datetime.combine(day, dtime(hour, minute))
        if at <= now <= at + timedelta(hours=catch_up_hours):
            return day.isoformat()
    return None


def offline(error: BaseException) -> bool:
    """ネットにつながらない（名前を引けない、つながらない、待ちきれない）ときの例外か。"""
    return isinstance(error, (aiohttp.ClientConnectionError, ConnectionError, TimeoutError, socket.gaierror))


class Scheduler:
    def __init__(self, config: Config, store: Store, assistant: Assistant):
        self.config = config
        self.store = store
        self.assistant = assistant
        # 取り込んだ新しい版と、それを最初に見つけた時刻
        self._version_checked = 0.0
        self._newer: tuple[str, float] | None = None
        # ネットにつながらなくなった時刻（つながっている間は None）
        self._offline_since: float | None = None

    @property
    def overview_channel_name(self) -> str:
        return self.config.overview_channels[0]

    # ループ

    async def loop(self) -> None:
        while True:
            await self.safe_tick(datetime.now())
            await asyncio.sleep(60)

    async def safe_tick(self, now: datetime) -> None:
        """1分ごとの tick。ネットにつながらない間は、毎分の長いエラーの代わりに、始めと終わりを1行ずつ残す。"""
        try:
            await self.tick(now)
        except Exception as e:
            if not offline(e):
                log.exception("定期処理に失敗しました")
            elif self._offline_since is None:
                self._offline_since = time.time()
                log.warning("ネットにつながらないので、定期処理はつながるまで待ちます: %s: %s", type(e).__name__, e)
            return
        if self._offline_since is not None:
            log.info("ネットにつながったので、定期処理を続けます（%s止まっていました）",
                     format_duration(time.time() - self._offline_since))
            self._offline_since = None

    async def tick(self, now: datetime) -> None:
        sched = self.config.schedule
        if not sched.enabled:
            return
        await self.catch_up_deferred(now.timestamp())
        pending = {(payload.get("name"), payload.get("day"))
                   for _, payload in self.store.pending_deferred("schedule")}
        for name in task_names(self.config):
            catch_up = NIGHT_CATCH_UP_HOURS if name == "night" else sched.catch_up_hours
            # Slack（App Home）で変えた時刻を毎回読み直す。止めている処理は空文字
            hhmm = settings.schedule_time(self.config, name)
            day = due_day(now, hhmm, catch_up)
            if day is None or self.store.schedule_ran(name, day) or (name, day) in pending:
                continue
            # 実行中に次の tick で二重に動かないよう、先に記録する
            self.store.record_schedule(name, day, {"status": "running"})
            await self.run_or_defer(name, day, now.timestamp())
        await self.nudge_stale_threads()
        await self.notify_unrestarted(now)
        await self.module_ticks(now)

    async def module_ticks(self, now: datetime) -> None:
        """モジュールの見回り（class Module の tick）。毎分呼び、間隔はモジュールが決める。

        1つのモジュールが落ちても、ほかのモジュールは止めない。ネットにつながらないときだけは、
        safe_tick が始めと終わりを1行ずつ残せるよう、そのまま投げる。
        """
        for name, module in self.assistant.modules.items():
            tick = getattr(module, "tick", None)
            if tick is None:
                continue
            try:
                await tick(now)
            except Exception as e:
                if offline(e):
                    raise
                log.exception("モジュール「%s」の見回りが落ちました", name)

    async def run_task(self, name: str, day: str, record: bool = True) -> dict:
        log.info("定期処理を始めます: %s（%s）", name, day)
        try:
            # モジュールの定期処理と、モジュールが受け持つ本体の定期処理（core_schedules。Daily・振り返り）
            owner = (modules.schedule_owner(self.config.modules, name)
                     or modules.core_schedule_owner(self.config.modules, name))
            module = self.assistant.modules.get(owner.name) if owner is not None else None
            detail = await (module.run_schedule(name, day) if module is not None else getattr(self, f"run_{name}")(day))
        except Exception as e:
            log.exception("定期処理 %s が失敗しました", name)
            detail = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        if record:
            self.store.record_schedule(name, day, detail)
        return detail

    def task_provider(self, name: str) -> str | None:
        """定期処理が使う明示 provider。保守はモデルを使わない。"""
        if name == "maintenance":
            return None
        owner = (modules.core_schedule_owner(self.config.modules, name)
                 or modules.schedule_owner(self.config.modules, name))
        if owner is not None:
            # モジュールの処理は、そのモジュールの実行役の provider（AI を使わないモジュールなら要らない）
            return settings.selected_provider(self.config, self.store, owner.name) if owner.actor else None
        # 夜間の Task などは、研究テーマを受け持つモジュールの担当（無ければ動かさない）
        owner = themes.catch_all_module(self.config)
        return settings.selected_provider(self.config, self.store, owner) if owner else ""

    def can_run(self, name: str, now: float, provider: str | None = None) -> bool:
        provider = self.task_provider(name) if provider is None else provider
        return provider is None or bool(provider) and self.store.limit_until(provider) <= now

    async def run_or_defer(self, name: str, day: str, now: float) -> bool:
        """実行する。途中で契約の上限に当たったら、その日の分として残さず、明けてからやり直す。"""
        provider = self.task_provider(name)
        if provider == "":
            self.store.record_schedule(name, day, {"status": "provider_unselected"})
            return False
        if not self.can_run(name, now, provider):
            until = self.store.limit_until(provider)
        else:
            await self.run_task(name, day)
            until = self.store.limit_until(provider) if provider else 0.0
        if until > now:
            log.info("上限に当たったので、%s（%s）は明けてからやり直します", name, day)
            self.store.forget_schedule(name, day)
            self.store.defer_run("schedule", {"name": name, "day": day, "provider": provider}, until)
            return False
        return True

    async def catch_up_deferred(self, now: float) -> None:
        """上限で止まった決まった時刻の処理を、明けてからやり直す（猶予の時間を過ぎていても動かす）。"""
        for deferred_id, payload in self.store.due_deferred("schedule", now):
            self.store.finish_deferred(deferred_id)
            if not self.store.schedule_ran(payload["name"], payload["day"]):
                await self.run_or_defer(payload["name"], payload["day"], now)

    # 夜間の Task

    async def run_night(self, day: str) -> dict:
        notion = self.assistant.notion
        if notion is None:
            return {"status": "no_notion"}
        try:
            tasks = await asyncio.to_thread(notion.tonight_tasks, self.config.schedule.night_max_tasks)
        except NotionError as e:
            await self.assistant.notify_trouble(f"夜間の Task を Notion から読めなかったので、今夜は実行しません: {e}")
            return {"status": "error", "error": str(e)}
        ids = await self.assistant.channel_ids()
        done = []
        for task in tasks:
            try:
                done.append(await self._run_night_task(task, ids))
            except Exception as e:
                # 「実行中」のまま残ると二度と実行されないので、確認待ちに戻して知らせる
                log.exception("夜間の Task「%s」が止まりました", task.title)
                reason = f"途中で止まりました: {type(e).__name__}: {e}"
                await self.assistant.notify_trouble(f"夜間の Task「{task.title}」が{reason}")
                with suppress(NotionError):
                    await asyncio.to_thread(notion.update_task, task.id, "確認待ち", reason)
                done.append({"title": task.title, "status": "error", "reason": reason, "url": task.url})
        try:
            remaining = await asyncio.to_thread(notion.count_tonight_tasks)
        except NotionError:
            remaining = None
        return {"status": "done", "tasks": done, "remaining": remaining}

    async def _run_night_task(self, task: Task, ids: dict[str, str]) -> dict:
        notion = self.assistant.notion
        info = {"title": task.title, "url": task.url, "theme": ", ".join(task.theme_names)}
        theme = task.theme_names[0] if task.theme_names else None
        channel_name = theme
        if theme is None or channel_name not in ids:
            reason = "テーマを設定してください" if theme is None else f"テーマのチャンネル #{theme} に Kei Agent がいません"
            await asyncio.to_thread(notion.update_task, task.id, "確認待ち", reason)
            return {**info, "status": "確認待ち", "reason": reason}

        await asyncio.to_thread(notion.update_task, task.id, "実行中")
        body = await asyncio.to_thread(notion.page_markdown, task.id)
        channel = ids[channel_name]
        source = parse_slack_permalink(task.slack_url)
        message: dict = {}
        # 元のメッセージがテーマのチャンネルにあるときだけ、そのスレッドで続ける
        if source and source[0] == channel:
            message = await self.assistant.fetch_message(*source) or {}
        if message:
            message_ts = source[1]
            thread_ts = message.get("thread_ts") or message_ts
        else:
            message_ts = None
            resp = await self.assistant.slack.chat_postMessage(channel=channel, text=f"🌙 Task: {task.title}")
            thread_ts = resp["ts"]
            await asyncio.to_thread(notion.update_task, task.id, None, None,
                                    await self.assistant.permalink(channel, thread_ts))

        text = (
            "[🌙 夜間の Task] 依頼者は寝ているので、その場で聞き返せません。"
            "判断が必要なところまで進めたら、最後の行を「❓ 確認:」で始めて止めてください。\n\n"
            f"タイトル: {task.title}\n優先度: {task.priority or '-'} / 期日: {task.due or '-'}\n"
            f"Notion: {task.url}\n\n## 本文\n\n{body or '（なし）'}\n"
        )
        if message:
            text += f"\n## 元の Slack のメッセージ\n\n{clean_text(message.get('text', ''))}\n"
        req = Request(channel, channel_name, thread_ts, None, text, trigger="night", files=message.get("files") or [])
        result = await self.assistant.process(req)

        if result is None or result.is_error:
            status = "確認待ち"
            summary = "エラーで止まりました"
        else:
            shown, contract_failed = self.assistant.render_reply(result)
            status = "確認待ち" if contract_failed or AWAITING_MARKER in result.text else "完了"
            summary = "返答を利用者向けの形に整えられませんでした" if contract_failed else summarize(shown)
        await asyncio.to_thread(notion.update_task, task.id, status, summary)
        if status == "完了" and message_ts:
            await self.assistant.react_done(channel, message_ts)
        return {**info, "status": status, "summary": summary}

    # Daily と振り返り

    async def sync_meetings(self, events: list[dict], now: datetime, source: str) -> dict | str:
        """朝に読んだ会議を、共通ホームの予定カレンダーに足す（briefing.py）。"""
        return await briefing.sync_meetings(self.assistant, events, now, source)

    # 保守

    async def run_maintenance(self, day: str) -> dict:
        detail: dict = {"status": "done"}
        # 自己改善の worktree と作業用のフォルダは、自己改善のモジュールが片づける
        detail["removed"] = await asyncio.to_thread(
            maintenance.cleanup, self.config, maintenance.claude_projects_dirs(self.config), None)
        detail["notices"] = self.store.drop_old_notices(time.time() - NOTICE_RETENTION_DAYS * 86400)
        # 声をかけてもさらに同じ時間が過ぎた返事待ちは閉じる（放っておくと何日も残る）
        detail["awaits"] = self.store.forget_stale_awaits(
            time.time() - self.config.schedule.unanswered_hours * 2 * 3600)
        # エージェントの claude の会話も、セッションの記録と同じ日数で忘れる
        detail["agent_sessions"] = self.store.drop_old_agent_sessions(
            time.time() - self.config.maintenance.session_retention_days * 86400)
        # モジュールの記録は、モジュールが決めた日数で忘れる（kei_agent.api.Records）
        detail["module_records"] = self.store.drop_expired_module_records(time.time())
        if self.config.maintenance.backup:
            try:
                detail["backup"] = await maintenance.backup(self.config, day, self.store)
                await self._warn_unsaved(detail["backup"].get("agent_root") or {})
            except maintenance.BackupError as e:
                await self.assistant.notify_trouble(f"研究データのバックアップに失敗しました: {e}")
                detail = {**detail, "status": "error", "error": str(e)}
        return detail

    async def _warn_unsaved(self, agent: dict) -> None:
        """Kei Agent 側（状態の書き出しなど）が保存できていないときに知らせる。"""
        why = {
            "not_a_repo": "Git のリポジトリになっていません",
            "no_remote": "push 先（origin）が登録されていません",
        }.get(str(agent.get("status") or ""))
        if why:
            await self.assistant.notify_trouble(
                f"Kei Agent 側のデータ（状態の書き出しなど）が保存できていません: `{agent.get('path')}` が{why}。"
                "非公開のリポジトリを作って `git remote add origin <URL>` してください")

    # 授業（大学エージェント）

    async def morning_text(self, now: datetime) -> tuple[str, dict, list[str]]:
        """朝のまとめ（今日の時系列。briefing.py）。"""
        found = await briefing.build(self.assistant, now)
        return found.text, found.detail, list(found.notices)

    def failure_note(self, now: datetime, failed_now: list[str] | None = None) -> str:
        """前回の Daily から今朝までに、うまくいかなかった定期処理を1行で（briefing.py。#0-kei-agent に知らせる）。"""
        return briefing.failure_note(self.assistant, now, failed_now)

    async def notify_unrestarted(self, now: datetime) -> None:
        """取り込んだ新しい版で、1時間たっても起動し直していなければ、一度だけ知らせる。"""
        if now.timestamp() - self._version_checked < VERSION_CHECK_SECONDS:
            return
        self._version_checked = now.timestamp()
        disk = await asyncio.to_thread(version.on_disk)
        if not version.differs(disk):
            self._newer = None
            return
        if self._newer is None or self._newer[0] != disk:
            # 自己改善の取り込みは、静かになってから起動し直す。見つけたばかりなら、もう少し待つ
            self._newer = (disk, now.timestamp())
            return
        key = f"version:{disk}"
        if now.timestamp() - self._newer[1] < VERSION_CHECK_SECONDS or self.store.noticed(key):
            return
        await self.assistant.notify_trouble(f"新しい版（{disk}）を取り込みましたが、まだ起動し直していません。"
                                            "deploy/restart-all.sh で起動し直してください")
        self.store.record_notice(key)

    # 声かけ

    async def nudge_stale_threads(self) -> None:
        hours = self.config.schedule.unanswered_hours
        for row in self.store.threads_to_nudge(time.time() - hours * 3600):
            req = Request(row["channel"], row["channel_name"], row["thread_ts"], None, "")
            try:
                await self.assistant.post(
                    req, f"<@{self.config.allowed_user_id}> ⏰ 返事待ちのまま{hours}時間たちました。続けるときは、このスレッドに返信してください。"
                )
            except Exception:
                # アーカイブしたチャンネルなどに投稿できなくても、毎分やり直さない
                log.warning("返事待ちのスレッドに声をかけられません: #%s %s", row["channel_name"], row["thread_ts"], exc_info=True)
            self.store.mark_nudged(row["channel"], row["thread_ts"])


async def _run_once(name: str, record: bool) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    from kei_agent.configuration.config import load_config
    from kei_agent.execution.jobs import JobManager
    from kei_agent.storage.notion_hub import load_hub
    from kei_agent.storage.notion_store import load_notion

    config = load_config()
    store = Store(config.db_path)
    slack = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
    auth = await slack.auth_test()
    pueue = jobs.queue(config)
    await pueue.ensure_group()  # 夜間の Task がジョブを投入することがある
    assistant = Assistant(config, store, slack, JobManager(config, store, pueue),
                          os.environ["SLACK_BOT_TOKEN"], auth["user_id"],
                          notion=load_notion(config), team_url=auth.get("url", ""),
                          team_id=auth.get("team_id", ""), hub=load_hub(config))
    scheduler = Scheduler(config, store, assistant)
    day = date.today().isoformat()
    detail = await scheduler.run_task(name, day, record=record)
    print(json.dumps(detail, ensure_ascii=False, indent=2))


def main() -> None:
    """定期処理を今すぐ1回動かす（確認用）。"""
    from kei_agent.configuration.config import load_config

    parser = argparse.ArgumentParser(prog="kei-agent-schedule")
    parser.add_argument("name", choices=task_names(load_config()))
    parser.add_argument("--record", action="store_true", help="今日の分を実行済みとして記録する")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_run_once(args.name, args.record))


if __name__ == "__main__":
    main()
