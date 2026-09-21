"""決まった時刻の処理: 先行研究の新着、Daily、振り返りの材料、🌙 の夜間 Task、放置されたスレッドへの声かけ。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from kei_agent import course, maintenance, morning, research, settings, themes, work
from kei_agent.assistant import Assistant
from kei_agent.config import Config
from kei_agent.digest import DigestBuilder
from kei_agent.notion import NotionError
from kei_agent.notion_store import Note, Task, parse_slack_permalink, summarize
from kei_agent.request import Request
from kei_agent.slack_text import AWAITING_MARKER, clean_text
from kei_agent.store import Store

log = logging.getLogger(__name__)

# 実行する順番。夜間の Task の結果を Daily に載せるため、night を先にする
TASK_NAMES = ("night", "literature", "daily", "review", "maintenance")  # 実行する順。settings.SCHEDULE_NAMES と同じもの
# 夜間の Task は、朝に Mac が起きたときにも実行する
NIGHT_CATCH_UP_HOURS = 12
NO_NEW_PAPERS = "NO_NEW_PAPERS"
WEEKDAYS = "月火水木金土日"
# 締切が近いものを知らせるために、カレンダーを見に行く間隔（秒）
DUE_CHECK_SECONDS = 3600
# 「一度だけ知らせた」目印を残す日数（学期の終わりまで持たなくてよい）
NOTICE_RETENTION_DAYS = 60


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


def label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def search_keywords(claude_md: Path) -> list[str]:
    """テーマの CLAUDE.md の「## 検索キーワード」の箇条書きを読む。"""
    if not claude_md.exists():
        return []
    text = re.sub(r"<!--.*?-->", "", claude_md.read_text(encoding="utf-8"), flags=re.DOTALL)
    m = re.search(r"^## 検索キーワード\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return [line.strip()[2:].strip() for line in m.group(1).splitlines()
            if line.strip().startswith("- ") and line.strip()[2:].strip()]


class Scheduler:
    def __init__(self, config: Config, store: Store, assistant: Assistant):
        self.config = config
        self.store = store
        self.assistant = assistant
        # 締切が近いものを最後に見に行った時刻（起動直後に1回見る）
        self._due_checked = 0.0

    @property
    def overview_dir(self) -> Path:
        return self.config.overview_dir

    @property
    def overview_channel_name(self) -> str:
        return self.config.overview_channels[0]

    # ループ

    async def loop(self) -> None:
        while True:
            try:
                await self.tick(datetime.now())
            except Exception:
                log.exception("定期処理に失敗しました")
            await asyncio.sleep(60)

    async def tick(self, now: datetime) -> None:
        sched = self.config.schedule
        if not sched.enabled:
            return
        # Claude の契約の上限に達している間は、決まった時刻の処理も始めない（明けてからやり直す）
        if self.assistant.limited_until > now.timestamp():
            return
        await self.catch_up_deferred(now.timestamp())
        for name in TASK_NAMES:
            catch_up = NIGHT_CATCH_UP_HOURS if name == "night" else sched.catch_up_hours
            # Slack（App Home）で変えた時刻を毎回読み直す。止めている処理は空文字
            hhmm = settings.schedule_time(self.config, self.store, name)
            day = due_day(now, hhmm, catch_up)
            if day is None or self.store.schedule_ran(name, day):
                continue
            # 実行中に次の tick で二重に動かないよう、先に記録する
            self.store.record_schedule(name, day, {"status": "running"})
            if not await self.run_or_defer(name, day, now.timestamp()):
                return   # 上限に当たった。残りは明けてからにする
        await self.notify_due_soon(now)
        await self.nudge_stale_threads()

    async def run_task(self, name: str, day: str, record: bool = True) -> dict:
        log.info("定期処理を始めます: %s（%s）", name, day)
        try:
            detail = await getattr(self, f"run_{name}")(day)
        except Exception as e:
            log.exception("定期処理 %s が失敗しました", name)
            detail = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        if record:
            self.store.record_schedule(name, day, detail)
        return detail

    async def run_or_defer(self, name: str, day: str, now: float) -> bool:
        """実行する。途中で契約の上限に当たったら、その日の分として残さず、明けてからやり直す。"""
        await self.run_task(name, day)
        if self.assistant.limited_until > now:
            log.info("上限に当たったので、%s（%s）は明けてからやり直します", name, day)
            self.store.forget_schedule(name, day)
            self.store.defer_run("schedule", {"name": name, "day": day}, self.assistant.limited_until)
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
            summary = "エラーで止まりました: " + ("; ".join(result.errors) if result else "内部エラー")
        else:
            status = "確認待ち" if AWAITING_MARKER in result.text else "完了"
            summary = summarize(result.text)
        await asyncio.to_thread(notion.update_task, task.id, status, summary)
        if status == "完了" and message_ts:
            await self.assistant.react_done(channel, message_ts)
        return {**info, "status": status, "summary": summary}

    # 先行研究の新着

    async def run_literature(self, day: str) -> dict:
        ids = await self.assistant.channel_ids()
        last = self.store.last_schedule("literature", before_day=day)
        since = date.fromisoformat(day) - timedelta(days=1)
        if last:
            since = min(since, date.fromisoformat(last["day"]))
        results = {}
        for cwd in themes.theme_dirs(self.config):
            name = cwd.name
            if name not in ids:
                continue  # アーカイブしたテーマや、Kei Agent のいないテーマは見張らない
            keywords = search_keywords(cwd / "CLAUDE.md")
            if not keywords:
                results[name] = {"status": "no_keywords"}
                continue
            ws = themes.resolve(self.config, name)
            prompt = (
                f"[Kei Agent の定期処理: 先行研究の新着 {day}]\n"
                f"検索キーワード: {' / '.join(keywords)}\n\n"
                f"kei-agent:literature skill で、キーワードごとに arXiv を `--sort date` で検索し、{since.isoformat()} 以降に"
                "投稿された論文のうち、papers/ にまだ保存していないものを探してください。"
                "テーマの CLAUDE.md の前提に照らして関係のある論文だけを papers/ に保存し、1本ずつ要点と関係を報告してください。\n"
                f"関係のある新着が1本もなければ、返答は `{NO_NEW_PAPERS}` の1行だけにしてください。"
                "ジョブは投入しないでください。"
            )
            result = await self.assistant.run_detached(ws, name, prompt, "literature")
            # 「新着なし」の目印の前後に説明をつけることがあるので、目印が含まれていれば新着なしとみなす
            if not result.is_error and NO_NEW_PAPERS in result.text:
                results[name] = {"status": "no_new"}
                continue
            thread_ts = await self.assistant.publish(ids[name], name, ws, f"📚 先行研究の新着 {label(day)}", result)
            results[name] = {"status": "error" if result.is_error else "posted", "thread_ts": thread_ts}
        return {"status": "done", "themes": results}

    # Daily と振り返り

    async def _write_digest(self, kind: str, day: str, since: float, ids: dict[str, str]) -> Path:
        path = self.overview_dir / ".kei-agent" / "digest" / f"{day}-{kind}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        title = {"daily": f"Daily の材料 {day}", "review": f"振り返りの材料 {day}"}[kind]
        text = await DigestBuilder(self.config, self.store, self.assistant).build(
            since, time.time(), title, set(ids), domains=kind == "review")
        path.write_text(text, encoding="utf-8")
        return path

    async def _save_note(self, channel: str, thread_ts: str, title: str, kind: str, day: str,
                         markdown: str, file: str) -> Note | None:
        notion = self.assistant.notion
        if notion is None or not markdown.strip():
            return None
        try:
            link = await self.assistant.permalink(channel, thread_ts)
            return await asyncio.to_thread(notion.create_note, title, kind, day, markdown, link, file)
        except NotionError as e:
            await self.assistant.notify_trouble(f"{title} を Notion のノートに書けませんでした: {e}")
            return None

    async def run_daily(self, day: str) -> dict:
        ids = await self.assistant.channel_ids()
        channel = ids.get(self.overview_channel_name)
        if channel is None:
            return {"status": "no_channel"}
        last = self.store.last_schedule("daily", before_day=day)
        since = last["ran_at"] if last else time.time() - 86400
        digest = await self._write_digest("daily", day, since, ids)
        ws = themes.resolve(self.config, self.overview_channel_name)
        prompt = (
            f"[Kei Agent の定期処理: Daily {day}]\n"
            f"`{digest}` に前回の Daily からの材料があります。材料と、そこに書かれたスレッドのログや振り返りのファイル、"
            "Notion のノートと Task を読み、今日の議論の起点になる Daily を書いてください。\n\n"
            "次の順で、全体を1画面に収めてください。\n"
            "1. 前日の動き（スレッドとその結果、Notion に書かれた計画と考察の要点。テーマごと）\n"
            "2. 夜間に終わったジョブと Task\n"
            "3. 先行研究の新着のうち重要なもの（なければ一言）。検索キーワードがないテーマがあれば、決めるよう促す\n"
            "4. 今日考えるとよい問い（2〜3個。前日の振り返りと考察のノートを踏まえる）\n"
            "5. 確認待ちの Task、期日が近い Task とマイルストーン、止まっているテーマ、返事待ちのスレッド\n\n"
            "月曜なら、材料に書かれている研究時間の CSV から、人の時間と Kei Agent の稼働時間を重ねた"
            "折れ線グラフを作り、`outputs/` に保存してください（月曜以外は作らなくてよい）。\n\n"
            f"同じ内容を `daily/{day}.md` に保存してください。返答が Slack と Notion にそのまま載ります。"
        )
        result = await self.assistant.run_detached(ws, self.overview_channel_name, prompt, "daily")
        title = f"Daily {label(day)}"
        # 朝に読むものを1通にまとめる。チャンネルには今日の時系列、スレッドに Daily の中身
        timeline, gathered, notices = await self.morning_text(datetime.now())
        # 声の「速い道」は、聞かれてから取りに行かず、朝に決まったものを手元へ渡しておく
        self.assistant.notify_voice("schedule", text=timeline)
        thread_ts = await self.assistant.publish(
            channel, self.overview_channel_name, ws, f"{timeline}\n\n🌅 {title}", result)
        for key in notices:
            self.store.record_notice(key)
        note = None
        if not result.is_error:
            note = await self._save_note(channel, thread_ts, title, "Daily", day, result.text, f"daily/{day}.md")
        return {"status": "error" if result.is_error else "posted", "thread_ts": thread_ts,
                "notion_url": note.url if note else None, "morning": gathered}

    async def run_review(self, day: str) -> dict:
        ids = await self.assistant.channel_ids()
        channel = ids.get(self.overview_channel_name)
        if channel is None:
            return {"status": "no_channel"}
        since = datetime.combine(date.fromisoformat(day), dtime(0, 0)).timestamp()
        digest = await self._write_digest("review", day, since, ids)
        review_path = self.overview_dir / "reviews" / f"{day}.md"
        ws = themes.resolve(self.config, self.overview_channel_name)
        prompt = (
            f"[Kei Agent の定期処理: 振り返りの材料 {day}]\n"
            f"`{digest}` に今日の材料があります。材料と、そこに書かれたスレッドのログを読み、"
            f"Codex App で振り返るための材料を `reviews/{day}.md` に書いてください。形式:\n\n"
            f"```markdown\n# 振り返り {day}\n\n## 今日やったこと\n（研究・大学・仕事ごとに、何をして何が分かったか）\n\n"
            "## 振り返りの問い\n1. 今日分かったことは何か（〜について、など具体的に）\n2. 明日やることは何か\n\n"
            "## Codex での振り返り\n（ここに Codex で話した結論を書く）\n```\n\n"
            "返答は Slack に投稿されるので、**研究・大学・仕事の3つを同じ形で並べて**ください。\n"
            "1つの領域につき、`*研究*` のような見出しのあと、次の3行だけを書きます。\n"
            "- `✅` 今日片付いたこと（材料に無ければ、その領域は「なし」と1行）\n"
            "- `⏳` 途中のもの・残っている締切\n"
            "- `→ 明日:` 明日あるもの（授業、会議、締切）\n"
            "材料に大学や仕事の節が無い領域は、まるごと省いてください。そのあと `─` の行を挟み、"
            "「振り返りの問い」を2つ書きます。問いは、3つの領域を見たうえでの問いにしてください"
            "（例: 明日は授業が2コマあるので、研究にどこを充てるか）。ほかには何も書かないでください。\n"
            "このあと、このスレッドに振り返りの結論が貼られたら、その内容を "
            f"`reviews/{day}.md` の「Codex での振り返り」に追記し、追記したことだけを短く返してください。"
        )
        result = await self.assistant.run_detached(ws, self.overview_channel_name, prompt, "review")
        title = f"振り返り {label(day)}"
        thread_ts = await self.assistant.publish(
            channel, self.overview_channel_name, ws, f"🌙 振り返りの材料 {label(day)}", result
        )
        note = None
        if not result.is_error:
            markdown = review_path.read_text(encoding="utf-8") if review_path.exists() else result.text
            note = await self._save_note(channel, thread_ts, title, "振り返り", day, markdown, f"reviews/{day}.md")
            if note:
                self.store.link_notion(channel, thread_ts, note.id, "review")
        where = f"Notion の <{note.url}|{title}> の「Codex での振り返り」" if note else "ファイルの「Codex での振り返り」"
        footer = (
            f"Codex App で `{review_path}` を開いて振り返ってください。"
            f"結論はこのスレッドに貼るか、{where}に書くと、明日の Daily に反映されます。"
        )
        await self.assistant.post(Request(channel, self.overview_channel_name, thread_ts, None, ""), footer)
        return {"status": "error" if result.is_error else "posted", "thread_ts": thread_ts,
                "notion_url": note.url if note else None}

    # 保守

    async def run_maintenance(self, day: str) -> dict:
        detail: dict = {"status": "done"}
        # いま直している最中の worktree と一時ディレクトリは残す（SQLite は別スレッドから触れない）
        busy = self.store.improvements_in("working", "review", "restarting")
        keep_worktrees = frozenset(Path(r["worktree"]).name for r in busy if r["worktree"])
        keep_scratch = frozenset(r["thread_ts"] for r in busy)
        detail["removed"] = await asyncio.to_thread(
            maintenance.cleanup, self.config, maintenance.claude_projects_dir(), None,
            keep_worktrees, keep_scratch)
        detail["notices"] = self.store.drop_old_notices(time.time() - NOTICE_RETENTION_DAYS * 86400)
        # 声をかけてもさらに同じ時間が過ぎた返事待ちは閉じる（放っておくと何日も残る）
        detail["awaits"] = self.store.forget_stale_awaits(
            time.time() - self.config.schedule.unanswered_hours * 2 * 3600)
        # エージェントの claude の会話も、セッションの記録と同じ日数で忘れる
        detail["agent_sessions"] = self.store.drop_old_agent_sessions(
            time.time() - self.config.maintenance.session_retention_days * 86400)
        if self.config.maintenance.backup:
            try:
                detail["backup"] = await maintenance.backup(self.config, day, self.store)
                await self._warn_unsaved(detail["backup"].get("agent_root") or {})
            except maintenance.BackupError as e:
                await self.assistant.notify_trouble(f"研究データのバックアップに失敗しました: {e}")
                detail = {**detail, "status": "error", "error": str(e)}
        return detail

    async def _warn_unsaved(self, agent: dict) -> None:
        """Kei Agent 側（Daily・振り返り・backlog・状態）が保存できていないときに知らせる。"""
        why = {
            "not_a_repo": "Git のリポジトリになっていません",
            "no_remote": "push 先（origin）が登録されていません",
        }.get(str(agent.get("status") or ""))
        if why:
            await self.assistant.notify_trouble(
                f"Daily と振り返りが保存できていません: `{agent.get('path')}` が{why}。"
                "非公開のリポジトリを作って `git remote add origin <URL>` してください")

    # 授業（大学エージェント）

    async def course_channel(self) -> str | None:
        """#20_course の ID。Kei Agent がいなければ None。"""
        name = self.config.course_channels[0] if self.config.course_channels else ""
        return (await self.assistant.channel_ids()).get(name) if name else None

    async def morning_text(self, now: datetime) -> tuple[str, dict, list[str]]:
        """朝のまとめ（今日の時系列）。集められなかったものは黙って飛ばす。"""
        detail: dict = {}
        classes: list[dict] = []
        dues: list[dict] = []
        events: list[dict] = []
        if course.AGENT in self.assistant.agents:
            synced = await self.assistant.ask_course(course.SYNC_ASSIGNMENTS)
            detail["synced"] = synced.ok
            classes = (await self.assistant.ask_course(course.LIST_CLASSES)).data.get("items") or []
            dues = await self.assistant.course_due(course.DIGEST_DAYS, now) or []
        if work.AGENT in self.assistant.agents:
            reply = await self.assistant.ask_work(work.LIST_EVENTS, days=2)
            events = reply.data.get("items") or [] if reply.ok else []
        detail |= {"classes": len(classes), "dues": len(dues), "events": len(events)}
        # 朝に出した締切は、そのあと24時間前の知らせで繰り返さない。ただし記録するのは
        # Slack に出せたあと（出す前に記録すると、投稿に失敗したときに黙って消える）
        notices = [course.notice_key(item) for item in course.soon_items(dues, now)]
        return morning.text(classes, events, dues, now, self.morning_notes()), detail, notices

    def morning_notes(self) -> list[str]:
        """時刻の無いもの（先行研究の新着など）を、1行ずつ。"""
        notes = []
        last = self.store.last_schedule("literature")
        if last:
            themes_ = (json.loads(last["detail"] or "{}") or {}).get("themes") or {}
            posted = [name for name, got in themes_.items() if got.get("status") == "posted"]
            if posted:
                notes.append("先行研究の新着: " + "、".join(f"#{name}" for name in posted))
        return notes

    async def notify_due_soon(self, now: datetime) -> None:
        """締切まで24時間を切った課題を、1件ずつ1回だけ知らせる。"""
        if now.timestamp() - self._due_checked < DUE_CHECK_SECONDS:
            return
        channel = await self.course_channel()
        if channel is None:
            return
        items = await self.assistant.course_due(2, now)
        if items is None:
            # 取れなかったときは時計を進めない（1時間待たずに、次の tick で取り直す）
            return
        self._due_checked = now.timestamp()
        for item in course.soon_items(items, now):
            key = course.notice_key(item)
            if self.store.noticed(key):
                continue
            await self.assistant.slack.chat_postMessage(channel=channel, text=course.soon_text(item, now))
            self.assistant.notify_voice("due", title=item.get("title"), at=item.get("at"))
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

    from kei_agent.config import load_config
    from kei_agent.jobs import JobManager
    from kei_agent.notion_store import load_notion

    config = load_config()
    store = Store(config.db_path)
    slack = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
    auth = await slack.auth_test()
    pueue = research.pueue(config)
    await pueue.ensure_group()  # 夜間の Task がジョブを投入することがある
    assistant = Assistant(config, store, slack, JobManager(config, store, pueue),
                          os.environ["SLACK_BOT_TOKEN"], auth["user_id"],
                          notion=load_notion(config), team_url=auth.get("url", ""),
                          team_id=auth.get("team_id", ""))
    scheduler = Scheduler(config, store, assistant)
    day = date.today().isoformat()
    detail = await scheduler.run_task(name, day, record=record)
    print(json.dumps(detail, ensure_ascii=False, indent=2))


def main() -> None:
    """定期処理を今すぐ1回動かす（確認用）。"""
    parser = argparse.ArgumentParser(prog="kei-agent-schedule")
    parser.add_argument("name", choices=TASK_NAMES)
    parser.add_argument("--record", action="store_true", help="今日の分を実行済みとして記録する")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_run_once(args.name, args.record))


if __name__ == "__main__":
    main()
