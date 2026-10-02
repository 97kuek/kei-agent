"""時間記録のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- `/toggl`（on_slash_command）… このチャンネルで計測を始める・止める。引数なしは切り替え、start / stop で向きを決める。
  Slack へは3秒以内に返すので、返す文は手元の計測だけで決め、Toggl と共通ホームへの送信は裏に回す
- 固定した時間記録カード（on_action / on_view）… [開始]、計測中は [停止] と [メモを追加]。`/toggl` で動かしたときも
  カードの表示を合わせる。科目を選ぶチャンネル（設定の pick_course）では、始めるときに今学期の科目を選ぶ
- 見回り（tick）… 送れなかった記録を送り直す。前の名前のボタンのままのカードを描き直す
- 定期処理 toggl_import … Toggl のアプリで直接測った記録を「時間記録」に取り込む
- Daily と振り返りの材料（material）… 今週の人の時間（共通ホームの「時間記録」から）

計測は1人1本。別のチャンネルで始めると、前の計測はその時刻で止まる。カードを置くのは `kei-agent-module time cards`。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import suppress
from datetime import date, datetime, timedelta

from slack_sdk.errors import SlackApiError

from kei_agent.api import Core, NotionError, TogglAmbiguousWrite, TogglError, load_toggl, theme_name

from . import cards, importer
from .entries import DOMAINS, TOGGL_SENT, Entries, Entry, domain_of, prefixes_of

log = logging.getLogger(__name__)

# 科目を選ぶときに聞く相手（module.toml の [depends]）と、その仕事
COURSE, CURRENT_COURSES = "course", "list-current-courses"
STOP_WORDS, START_WORDS = ("stop", "停止"), ("start", "開始")
USAGE = "使い方: `/toggl`（切り替え）、`/toggl start`、`/toggl stop`"
# Slack が「その投稿（チャンネル）はもう無い」と答えたときの理由
GONE = ("message_not_found", "channel_not_found", "is_archived")


def gone(error: Exception) -> bool:
    """カードが消された（チャンネルが無くなった）か。つながらないだけのときは False。"""
    return isinstance(error, SlackApiError) and (error.response or {}).get("error") in GONE


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.entries = Entries(core.records)
        self.prefixes = prefixes_of(core.settings.get("prefixes"))
        self.pick_course = frozenset(str(name) for name in core.settings.get("pick_course") or [])
        # 描いたカードのボタンの名前の頭。変わっていたら（本体から写したカードなど）描き直す
        self.buttons = core.action_id("")
        self._cards_checked = False
        self._retrying = False

    def domain(self, channel_name: str) -> str:
        """チャンネルの Slack での名前から、時間の領域。測らないチャンネルなら空文字。"""
        return domain_of(self.prefixes, channel_name)

    # /toggl

    async def on_slash_command(self, name: str, body: dict) -> str:
        user_id = str(body.get("user_id") or "")
        channel, raw = str(body.get("channel_id") or ""), str(body.get("channel_name") or "")
        if channel and (not raw or raw in ("privategroup", "directmessage")):
            raw = await self.core.channel_name(channel)
        domain = self.domain(raw) if channel else ""
        if not domain:
            return f"時間を記録できるのは {'・'.join(sorted(self.prefixes))} で始まるチャンネルだけだよ。"
        arg = str(body.get("text") or "").strip().lower()
        if arg not in ("", *START_WORDS, *STOP_WORDS):
            return USAGE
        active = self.entries.active(user_id)
        here = active is not None and active.channel == channel
        if arg in STOP_WORDS or (not arg and here):
            entry = self.entries.stop(user_id)
            if entry is None:
                return "いまは計測していないよ。"
            self.core.spawn(self._after_change(stopped=entry, post_cards=False))
            return f"⏹ 止めたよ: {entry.description}（{entry.minutes}分）"
        if here:
            return f"⏱️ もう計測中だよ: {active.description}"
        theme = theme_name(raw)
        if domain == "course" and theme in self.pick_course:
            await self._open_course_picker(str(body.get("trigger_id") or ""), channel)
            return "科目を選ぶ画面を開いたよ。選ぶと計測を始めるね。"
        entry, previous = self.entries.start(user_id, domain, channel, theme)
        self.core.spawn(self._after_change(started=entry, stopped=previous, post_cards=False))
        switched = f"（{previous.description} は止めたよ）" if previous else ""
        return f"▶️ 始めたよ: {entry.description}{switched}"

    # 頭（手の口の timer）から測る。カードは置かない（Slack にいないとき、カードは知らせにたまるだけになる）

    async def head_action(self, name: str, params: dict) -> dict | None:
        if name != "timer":
            return None
        action = str(params.get("action") or "")
        user_id = self.core.owner_id
        started = stopped = None
        if action == "start":
            domain, label = str(params.get("domain") or ""), str(params.get("label") or "").strip()
            if domain not in DOMAINS or not label:
                raise ValueError(f"始めるときは domain（{' / '.join(DOMAINS)}）と label（テーマや科目の名前）を渡してください")
            channel = await self._head_channel(domain, label)
            # 名前は頭が言ったラベル（大学なら、そのチャンネルで選んでいた科目より、頭が言った科目を使う）
            started, stopped = self.entries.start(user_id, domain, channel, theme_name(label), label=label)
        elif action == "stop":
            stopped = self.entries.stop(user_id)
        elif action != "status":
            raise ValueError("action は start / stop / status のどれかにしてください")
        # カードのあるチャンネル（Slack で測っていたもの）は表示を合わせる。カードの無いところには置かない
        await self._after_change(started=started, stopped=stopped, post_cards=False)
        return {"running": _shown(self.entries.active(user_id)), "stopped": _shown(stopped)}

    async def _head_channel(self, domain: str, label: str) -> str:
        """頭から測るときの置き場所（Toggl の確認の知らせを出す先）。研究はテーマの、大学・仕事は担当のチャンネル。"""
        names = self.core.channels(domain)
        name = theme_name(label) if domain == "research" or not names else names[0]
        return (await self.core.channel_ids()).get(name, name)

    # カードのボタンと画面

    async def on_action(self, name: str, body: dict) -> None:
        action = (body.get("actions") or [{}])[0]
        value = str(action.get("value") or "")
        user_id = str((body.get("user") or {}).get("id") or "")
        where = body.get("channel") or {}
        channel = str(where.get("id") or (body.get("container") or {}).get("channel_id") or "")
        if not channel:
            return
        if name == cards.START:
            raw = str(where.get("name") or "")
            if not raw or raw == "privategroup":
                # 番号を外したテーマ名では領域を見分けられないので、Slack の生の名前を使う
                raw = await self.core.channel_name(channel)
            domain = self.domain(raw)
            if not domain:
                return
            if domain == "course" and theme_name(raw) in self.pick_course:
                await self._open_course_picker(str(body.get("trigger_id") or ""), channel)
                return
            entry, previous = self.entries.start(user_id, domain, channel, theme_name(raw))
            await self._after_change(started=entry, stopped=previous)
        elif name == cards.STOP:
            active = self.entries.active(user_id)
            if active is None or active.id != value:
                # 古いカードの停止ボタン。別のチャンネルで動いている計測は止めず、このカードだけ直す
                await self._update_card(channel, active if active is not None and active.channel == channel else None)
                return
            entry = self.entries.stop(user_id)
            if entry is not None:
                await self._after_change(stopped=entry)
        elif name == cards.MEMO:
            entry = self.entries.entry(value)
            if entry is not None and entry.user_id == user_id:
                await self.core.open_view(str(body.get("trigger_id") or ""), cards.memo_view(self.core.view_id, entry))
        elif name == cards.RETRY:
            entry = self.entries.entry(value)
            if entry is not None and entry.user_id == user_id:
                await self._sync(entry, force=True)

    async def on_view(self, name: str, body: dict) -> dict | None:
        view = body.get("view") or {}
        values = (view.get("state") or {}).get("values") or {}
        user_id = str((body.get("user") or {}).get("id") or "")
        if name == cards.MEMO_VIEW:
            entry = self.entries.entry(str(view.get("private_metadata") or ""))
            if entry is None or entry.user_id != user_id:
                return {"memo": "この記録はもうありません"}
            self.entries.add_memo(entry.id, str(((values.get("memo") or {}).get("text") or {}).get("value") or ""))
            return None
        if name == cards.COURSE_VIEW:
            channel = str(view.get("private_metadata") or "")
            chosen = ((values.get("course") or {}).get("select") or {}).get("selected_option") or {}
            try:
                picked = json.loads(str(chosen.get("value") or ""))
                course_id, course_name = str(picked["id"]), str(picked["name"])
            except (ValueError, KeyError, TypeError):
                return {"course": "科目を選び直してね"}
            # Slack は3秒以内の返事を求める。Toggl や Notion への送信は、画面を閉じたあとに回す
            self.core.spawn(self._start_course(user_id, channel, course_id, course_name))
        return None

    async def _open_course_picker(self, trigger_id: str, channel: str) -> None:
        """先に読み込み中の画面を開き、今学期の科目が届いたら差し替える（trigger_id は3秒で切れる）。"""
        opened = await self.core.open_view(trigger_id, cards.course_loading_view(self.core.view_id, channel))
        self.core.spawn(self._fill_course_picker(str(opened.get("id") or ""), channel))

    async def _fill_course_picker(self, view_id: str, channel: str) -> None:
        reply = await self.core.ask_module(COURSE, CURRENT_COURSES, {})
        items = reply.data.get("items") if reply.ok else None
        view = (cards.course_view(self.core.view_id, channel, items) if items else cards.course_loading_view(
            self.core.view_id, channel, "今学期の履修科目を読み出せなかったよ。大学のモジュールと Notion の「授業」を確認してね。"))
        await self.core.update_view(view_id, view)

    async def _start_course(self, user_id: str, channel: str, course_id: str, course_name: str) -> None:
        self.entries.bind_course(channel, course_id, course_name)
        entry, previous = self.entries.start(user_id, "course", channel, theme_name(await self.core.channel_name(channel)))
        await self._after_change(started=entry, stopped=previous)

    # カードと送信

    async def _after_change(self, started: Entry | None = None, stopped: Entry | None = None,
                            post_cards: bool = True) -> None:
        """カードの表示を合わせ、止めた記録を Toggl と共通ホームに送る。"""
        if stopped is not None:
            await self._update_card(stopped.channel, None, create=post_cards)
        if started is not None:
            await self._update_card(started.channel, started, create=post_cards)
        if stopped is not None:
            await self._sync(stopped)

    async def _update_card(self, channel: str, entry: Entry | None, create: bool = True) -> None:
        """カードを今の計測に合わせる。create=False なら、カードのないチャンネルには置かない（/toggl のとき）。"""
        ts = self.entries.card(channel)
        if not ts and not create:
            return
        text, blocks = cards.fallback_text(entry), cards.blocks(self.core.action_id, entry)
        if ts:
            try:
                await self.core.update(channel, ts, text, blocks=blocks)
            except Exception:
                # カードが消されたときは、新しく置き直す（固定は利用者がやり直す）
                log.warning("時間カードを更新できないので置き直します: %s", channel, exc_info=True)
            else:
                self.entries.set_card(channel, ts, self.buttons)
                return
        self.entries.set_card(channel, await self.core.post(channel, text, blocks=blocks), self.buttons)

    async def _sync(self, entry: Entry, force: bool = False) -> None:
        """止めた記録を Toggl と共通ホームの「時間記録」に送る。送れなければ保留にして、見回りで送り直す。

        Toggl に届いたか分からない（送ったあとに切れた）ときだけは、二重に入れないよう、利用者が確かめてから
        [Togglへ再送] を押すのを待つ。
        """
        if entry.ended_at is None or (entry.toggl_state == "needs_review" and not force):
            return
        if entry.toggl_state not in TOGGL_SENT:
            toggl = load_toggl()
            if toggl is None:
                # Toggl を使わない運用でも、共通ホームの時間記録には残す
                entry = self.entries.set_delivery(entry.id, toggl_state="not_configured")
            else:
                try:
                    await self.core.to_thread(toggl.record_completed, entry.description, entry.description,
                                              datetime.fromtimestamp(entry.started_at).astimezone(),
                                              max(1, int(entry.ended_at - entry.started_at)))
                except TogglAmbiguousWrite:
                    self.entries.set_delivery(entry.id, toggl_state="needs_review")
                    await self.core.post(entry.channel, "Toggl の確認が必要です",
                                         blocks=cards.retry_blocks(self.core.action_id, entry))
                    return
                except TogglError as e:
                    log.warning("Toggl に送れないので後で再送します: %s", e)
                    self.entries.set_delivery(entry.id, toggl_state="pending")
                    return
                entry = self.entries.set_delivery(entry.id, toggl_state="done")
        if entry.notion_state == "done":
            return
        hub = self.core.hub
        if hub is None or not hub.has_time_db:
            # 共通ホームが使えない間は保留にして、使えるようになってから送る（知らせは本体が起動したときの1回だけ）
            if entry.notion_state != "pending":
                self.entries.set_delivery(entry.id, notion_state="pending")
            return
        slack_url = ""
        if ts := self.entries.card(entry.channel):
            with suppress(Exception):
                slack_url = await self.core.permalink(entry.channel, ts)
        started_at = datetime.fromtimestamp(entry.started_at).astimezone().isoformat()
        try:
            await self.core.to_thread(hub.record_time, entry.id, entry.domain, entry.label, started_at,
                                      entry.minutes, entry.memo, slack_url, "Slack")
        except Exception:
            log.warning("Notion の時間記録は後で再試行します", exc_info=True)
            self.entries.set_delivery(entry.id, notion_state="pending")
        else:
            self.entries.set_delivery(entry.id, notion_state="done")

    # 見回り・定期処理・材料

    async def tick(self, now: datetime) -> None:
        """毎分。送れなかった記録の再送とカードの描き直しは、定期処理を待たせないよう裏で動かす（1本ずつ）。"""
        if not self._retrying:
            self._retrying = True
            self.core.spawn(self._look_around())

    async def _look_around(self) -> None:
        try:
            if not self._cards_checked:
                self._cards_checked = True
                await self._redraw_cards()
            # ネットワークが切れたなどで保留になった記録だけ。Toggl に届いたか分からないものは利用者を待つ
            for entry in self.entries.pending():
                if entry.toggl_state != "needs_review":
                    await self._sync(entry)
        finally:
            self._retrying = False

    async def _redraw_cards(self) -> None:
        """前の名前のボタンのままのカード（本体が持っていたころに置いたものなど）を、今のボタンで描き直す。

        起動して最初の見回りで1回だけ。消されていたカードは、置き直さずに忘れる（要るなら
        `kei-agent-module time cards` で置く）。つながらなかったものは、次に起動したときにもう一度試す。
        """
        for card in self.entries.cards():
            if card.get("buttons") == self.buttons:
                continue
            channel, ts = str(card.get("channel") or ""), str(card.get("ts") or "")
            entry = self.entries.running_in(channel)
            try:
                await self.core.update(channel, ts, cards.fallback_text(entry),
                                       blocks=cards.blocks(self.core.action_id, entry))
            except Exception as e:
                if gone(e):
                    log.info("時間カードが消されていたので忘れます: %s", channel)
                    self.entries.forget_card(channel)
                else:
                    log.warning("時間カードを描き直せませんでした: %s", channel, exc_info=True)
            else:
                self.entries.set_card(channel, ts, self.buttons)

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
                         "- 人: 今週はまだ記録がない（Slack の /toggl か時間記録カードで測る。"
                         f"Toggl で直接測るならプロジェクト名の先頭に `{marks}/`）")
        if url := hub.time_url():
            lines.append(f"- 時間記録（週ごとのグラフ）: {url}")
        return lines


def _shown(entry: Entry | None) -> dict | None:
    """頭に見せる計測（説明・始めた時刻・分）。"""
    if entry is None:
        return None
    return {"description": entry.description, "started": datetime.fromtimestamp(entry.started_at).strftime("%H:%M"),
            "minutes": entry.minutes if entry.ended_at is not None else int((time.time() - entry.started_at) // 60)}
