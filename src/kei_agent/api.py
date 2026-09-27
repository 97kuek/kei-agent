"""モジュールがコアとやり取りする窓口（枠の版 1。docs/extensibility.md の「コアとモジュール」）。

モジュールの Python（modules/<名前>/module.py）が読み込んでよい Kei Agent の部品は、この kei_agent.api だけ
（同じフォルダのファイルは `from . import texts` のように読める）。ここに無いものに頼ると、コアを直したときに
動かなくなる。枠を変えるときは modules.API_VERSION を上げる。

module.py には `class Module` を置き、`__init__(self, core)` で窓口（Core）を受け取る。使う差し込み口だけを書く。

- `async on_message(req, skill="", params=None)` … モジュールのチャンネルと、claim_thread したスレッドへの
  依頼者の書き込み。研究全体のチャンネルから回ってきたときは、振り分け係が選んだ仕事が skill と params に入る
  （空なら core.pick_skill で選べる）。答えは core.reply か core.converse で返す（どちらも依頼の 👀 を ✅ に
  変える。例外を投げたら ⚠️ と知らせ）。[channels] があれば必須
- `async on_reaction(event, added) -> bool` … リアクションの付け外し（Slack の reaction_added の中身）。
  自分の投稿へのものなら扱って True を返す（ほかのモジュールと 🌙 には回らない）
- `async run_schedule(name, day) -> dict` … module.toml の [schedules] の処理（day は YYYY-MM-DD）。
  返した辞書は記録に残り、{"status": "error"} なら朝の一覧の「うまくいかなかったこと」に載る。[schedules] があれば必須
- `async tick(now)` … 毎分呼ばれる見回り（締切の知らせなど）。間隔はモジュールが決める（例: 1時間に1回だけ見る）
- `morning_notes(day) -> list[str]` … 朝の一覧（Daily の投稿）に足す行
- `async agenda(days, kinds=None) -> list[dict] | None` … これから days 日の、時刻のある予定。朝の一覧・
  声のレイヤ・振り返りの材料に載る。kinds が来たら、その種類だけでよい（重い読み取りを省ける）。1件は
  会議 `{"kind": "meeting", "subject", "start": "YYYY-MM-DDTHH:MM", "end", "location", "url", "id", "source"}`
  （共通ホームの予定カレンダーにも source を出典として書く）、授業 `{"kind": "class", "subject", "start", "end"}`、
  締切 `{"kind": "due", "title", "course", "at", "url", "id", "notice"}`（notice は、朝の一覧に出したら記録する
  目印。24時間前の知らせで core.notice_once(notice) を使えば、朝に出したものを繰り返さない）。
  読めなかったら None を返す（空の [] と分ける。予定カレンダーの行を「要確認」にしないため）
- `async prepare(kind, day) -> list[str]` … Daily（kind = "daily"）と振り返り（"review"）の前の取り込み。
  うまくいかなかったことの短い名前（例: "課題の取り込み"）を返すと、朝の一覧の「うまくいかなかったこと」に載る
- `async on_event(kind, data)` … 本体やほかのモジュールが配った出来事（core.emit）。受け取ったら自分で扱う
  （声なら喋る）。投げっぱなしなので、返事は要らない。出来事の種類は docs/extensibility.md の「出来事」
- `home() -> list[dict]` … App Home に出す、このモジュールの項目（Slack の blocks。見出しは本体が付ける）。
  押せるものの action_id は core.home_action_id(名前) で作る（チェックなら core.home_checkboxes）
- `async on_home_action(name, action)` … App Home の、このモジュールの項目が押されたとき（name は
  home_action_id に渡した名前、action は Slack の action）。依頼者のときだけ呼ばれ、終わると App Home を作り直す
- `welcome() -> str` … モジュールのチャンネルに招かれたときの案内（できること）
- `default_question` … 本文の無いメンションのときに、担当に聞くこと
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent import agents, dates, deadline, home, modules, router, settings, themes
from kei_agent.agents import Reply
from kei_agent.calendar_sync import JST, CalendarItem, CalendarSnapshot, IncompleteSnapshot, sync_calendar
from kei_agent.notion import NotionError
from kei_agent.records import Records
from kei_agent.request import Request
from kei_agent.response_output import OutputError, safe_failure, validate_structured_response
from kei_agent.slack_text import escape, split_text

if TYPE_CHECKING:
    from kei_agent.assistant import Assistant

log = logging.getLogger(__name__)
API_VERSION = modules.API_VERSION
__all__ = ["API_VERSION", "ASK", "Core", "NotionError", "Records", "Reply", "Request", "Theme", "checked_text",
           "day_label", "due_clock", "due_day", "escape", "failure_text", "parse_time", "selected_values", "weekday"]
# 定型に当てはまらない質問の窓口（どの担当の名刺でも同じ名前）
ASK = router.ASK


def failure_text(kind: str = "connection") -> str:
    """失敗したときに Slack に出す、決まった文（connection / timeout / login など。中の詳しいことは書かない）。"""
    return safe_failure(kind)


def day_label(day: str | date) -> str:
    """9/25（金）の形。YYYY-MM-DD の文字列も渡せる。"""
    return dates.day_label(date.fromisoformat(day) if isinstance(day, str) else day)


def parse_time(value: object) -> datetime | None:
    """予定・締切の時刻（ISO の文字列）を読む。読めなければ None。"""
    return dates.parse_time(value)


def weekday(day: date) -> str:
    """曜日の1文字（月〜日）。"""
    return dates.weekday(day)


def due_day(at: datetime) -> date:
    """締切の日。0:00 ちょうどは前の日の終わり（Moodle の「24:00」）として扱う。"""
    return deadline.day(at)


def due_clock(at: datetime) -> str:
    """締切の時刻の書き方（0:00 ちょうどは前の日の 24:00）。"""
    return deadline.clock(at)


def selected_values(action: dict) -> set[str]:
    """App Home で押された action の、選ばれている値（チェックなら付いているもの全部、選ぶ形なら1つ）。"""
    chosen = {str(option.get("value")) for option in action.get("selected_options") or []}
    if action.get("selected_option"):
        chosen.add(str(action["selected_option"].get("value")))
    return chosen


def checked_text(text: str) -> str | None:
    """担当が返した文を、Slack に出せる形か確かめる（手元のパスや作業の実況を出さない）。出せなければ None。"""
    try:
        return validate_structured_response(text)
    except OutputError:
        return None


@dataclass(frozen=True)
class Theme:
    """研究テーマ（研究がモジュールになるまでは、コアが持つ）。"""
    name: str
    path: Path
    # テーマの CLAUDE.md の「## 検索キーワード」と、CLAUDE.md の本文（前提）
    keywords: tuple[str, ...]
    premises: str


class Core:
    """1つのモジュールのための窓口。Slack・担当・記録・定期処理の記録・研究テーマに、決めた形でだけ触れる。"""

    def __init__(self, assistant: Assistant, spec: modules.ModuleSpec):
        self._assistant = assistant
        self.spec = spec
        self.name = spec.name
        self.records = Records(assistant.store, spec.name)

    @property
    def settings(self) -> dict:
        """このモジュールの設定（module.toml の [settings] の既定に、config.toml の [<名前>] を重ねた写し）。"""
        return self._assistant.config.settings(self.name)

    # Notion（Notion のモジュールができるまでは、コアの接続をそのまま渡す。使えなければ None）

    @property
    def hub(self):
        """共通ホーム（kei_agent.notion_hub.HubStore）。"""
        return self._assistant.hub

    @property
    def notion(self):
        """研究ホーム（kei_agent.notion_store.NotionStore）。"""
        return self._assistant.notion

    # Slack

    def is_owner(self, user_id: str | None) -> bool:
        """依頼者本人か（Kei Agent に指示できるのは、この人だけ）。"""
        return self._assistant.is_allowed(user_id)

    async def channel_ids(self) -> dict[str, str]:
        """Kei Agent がいるチャンネル（番号を外した名前 → ID）。"""
        return await self._assistant.channel_ids()

    def home_action_id(self, name: str) -> str:
        """App Home に出す、押せるものの action_id（押されると、このモジュールの on_home_action(name, action)）。"""
        return home.module_action_id(self.name, name)

    def home_checkboxes(self, name: str, options: dict[str, str], chosen: set[str]) -> dict:
        """App Home に出すチェック（値 → 表示名）。付いているものは chosen。押されると on_home_action(name, action)。"""
        return home.checkboxes(self.home_action_id(name), options, chosen)

    def channels(self, kind: str) -> tuple[str, ...]:
        """module.toml の [channels] の種類に当たるチャンネルの名前（設定の [channels] で変えたものも）。"""
        return self._assistant.config.module_channels.get(kind, ())

    async def post(self, channel: str, text: str, *, thread_ts: str | None = None) -> str:
        """投稿する（リンクのプレビューは付けない）。投稿の ts を返す。"""
        where = {"thread_ts": thread_ts} if thread_ts else {}
        posted = await self._assistant.slack.chat_postMessage(channel=channel, text=text, unfurl_links=False,
                                                              unfurl_media=False, **where)
        return str(posted.get("ts") or "")

    async def react(self, channel: str, ts: str, emoji: str, *, remove: bool = False) -> None:
        """リアクションを付ける（remove なら外す）。付け外しに失敗しても止めない。"""
        slack = self._assistant.slack
        await self._assistant._react(slack.reactions_remove if remove else slack.reactions_add, channel, ts, emoji)

    async def reply(self, req: Request, text: str, *, failed: bool = False) -> None:
        """依頼のスレッドに答える（依頼の 👀 を ✅ に、failed なら ⚠️ に変える）。AI の担当に答えさせるなら converse。"""
        for chunk in split_text(text):
            await self._assistant.post(req, chunk)
        await self._assistant.mark_answered(req, failed=failed)

    def watch_thread(self, channel: str, ts: str, channel_name: str) -> None:
        """そのスレッドへの返信を、メンションなしでも拾う（投稿した本人が Kei Agent なので、元の投稿も会話に渡る）。"""
        self._assistant.store.upsert_thread(channel, ts, channel_name, None)

    def claim_thread(self, channel: str, ts: str, channel_name: str) -> None:
        """そのスレッドの続きを、このモジュールの on_message が受ける（研究テーマのチャンネルでも）。"""
        self.watch_thread(channel, ts, channel_name)
        self._assistant.store.set_agent_session(channel, ts, self.name, "")

    async def notify_trouble(self, text: str) -> None:
        """困りごとを Kei Agent の改善のチャンネルに知らせる。"""
        await self._assistant.notify_trouble(text)

    def noticed(self, key: str) -> bool:
        """その目印で、もう知らせたか（知らせたあとに mark_noticed で残す。出す前に残すと、失敗したときに黙って消える）。"""
        return self._assistant.store.noticed(f"module.{self.name}.{key}")

    def mark_noticed(self, key: str) -> None:
        self._assistant.store.record_notice(f"module.{self.name}.{key}")

    def emit(self, kind: str, **data) -> None:
        """出来事を配る（締切が近い、など）。受け取るのは on_event を持つモジュール（声なら喋る）。

        投げっぱなしで、誰も受け取らなくても、受け取った側が落ちても、呼んだ側は気にしなくてよい。
        """
        self._assistant.emit(kind, **data)

    def notice_once(self, key: str) -> bool:
        """その目印でまだ知らせていなければ True を返し、知らせたことにする（同じことを何度も知らせない）。

        目印は毎晩の保守で60日たつと消えるので、直っていなければ、そのころにもう一度知らせる。
        """
        store = self._assistant.store
        mark = f"module.{self.name}.{key}"
        if store.noticed(mark):
            return False
        store.record_notice(mark)
        return True

    # 担当（module.toml の [process] と [actor]）

    async def ask_agent(self, skill: str, payload: dict) -> Reply:
        """このモジュールの担当プロセス（[process]）に仕事を頼む。材料は本文の JSON で渡す（ログには残さない）。

        うまくいかなければ理由を知らせ、利用上限に当たったら覚えておく（定期処理は明けてからやり直す）。
        """
        agent = self._assistant.agents.get(self.name)
        if agent is None:
            await self.notify_trouble(f"{self.spec.label}の担当の住所がありません（module.toml の [process]）")
            return Reply.broken(f"{self.spec.label}の担当の住所がないよ")
        provider = settings.selected_provider(self._assistant.config, self._assistant.store, self.name)

        async def keep_alive(_status: str) -> None:
            # AI を動かす仕事なので、経過を流しながら受け取る（途中で切られないように）
            return None

        reply = await agents.ask(agent, skill, params={"provider": provider}, on_progress=keep_alive,
                                 text=json.dumps(payload, ensure_ascii=False))
        if not reply.ok:
            await self.notify_trouble(f"{self.spec.label}の担当（{agent.base_url}）の {skill} が返した理由: "
                                      f"{reply.text[:300]}")
        await self._assistant.note_limit(reply, self.name, provider)
        return reply

    async def tell_agent(self, skill: str, payload: dict) -> bool:
        """このモジュールの担当プロセスに、知らせだけを渡す（AI を動かさない、すぐ終わる仕事。声なら喋る）。

        うまくいかなくても困りごととしては知らせず、ログに残すだけ（担当が止まっていても、本体の仕事は終わっている）。
        住所が無ければ何もしない。届いたら True。
        """
        agent = self._assistant.agents.get(self.name)
        if agent is None:
            return False
        reply = await agents.ask(agent, skill, text=json.dumps(payload, ensure_ascii=False))
        if not reply.ok:
            log.info("%sの担当に %s を渡せませんでした: %s", self.spec.label, skill, reply.text[:200])
        return reply.ok

    async def skills(self) -> list[dict]:
        """この担当の名刺に載っている仕事（読めなければ空）。"""
        return await self._assistant.skills_of(self.name)

    async def pick_skill(self, req: Request) -> tuple[str, dict]:
        """言われたことが、この担当の名刺のどの仕事に当たるかを軽いモデルで選ぶ（選べなければ ASK）。

        返すのは仕事の id と、振り分け係が拾った指定（days・limit）。
        """
        skills = await self._assistant.skills_of(self.name)
        if not skills:
            return ASK, {}
        await self._assistant.thread_ui(req).activity(router.STATUS_TEXT)
        choice = await router.pick(self._assistant.config, skills, req.text, store=self._assistant.store)
        return choice.skill or ASK, choice.params

    async def work(self, req: Request) -> None:
        """研究テーマのチャンネル（[channels] に "*" で受け持つもの）で、そのチャンネルの作業場を使って、
        このモジュールの担当と会話して答える。

        添付の保存・できたファイルの添付・接続先の許可・引き継ぎの提案・ジョブは、研究と同じ流れ。担当のプロセスには
        ask の依頼に channel_name（作業場）と allowed_domains（許可済みの接続先）が添えて届く。
        """
        await self._assistant.work_in_workspace(req, self.name)

    async def converse(self, req: Request) -> None:
        """そのスレッドの会話として、このモジュールの担当（[actor] と [process]）に聞いて答える。

        会話の続き・経過の表示・上限に当たったときのやり直し・出力の確認は、大学や仕事の担当と同じ。
        """
        await self._assistant.converse_with_agent(req, self.name)

    # 共通ホームの予定カレンダー

    async def sync_calendar(self, source: str, items: list[dict], *, day: str, days: int, complete: bool,
                            expected_count: int | None = None) -> dict | str:
        """共通ホームの予定カレンダーに、出典 source の予定を写す（出典と ID で照合し、手入力の行には触らない）。

        items の1件は {"id", "title", "start", "end", "url", "location", "status"}（start は日付か日時）。
        day（YYYY-MM-DD）から days 日の中で見えなくなった行は、消さずに「要確認」にする。全部を読めたと
        言い切れるとき（complete）だけ、0件も「全部なくなった」と扱う。返すのは件数（作った・直した・要確認）か、
        "no_hub"（共通ホームが無い）・"error"（写せなかった。理由はログ）。
        """
        hub = self._assistant.hub
        if hub is None:
            return "no_hub"
        checked_at = datetime.combine(date.fromisoformat(day), dtime(9, 0), JST)
        try:
            rows = tuple(CalendarItem(source_id=str(item.get("id") or ""), title=str(item.get("title") or ""),
                                      start=str(item.get("start") or ""), end=str(item.get("end") or ""),
                                      url=str(item.get("url") or ""), location=str(item.get("location") or ""),
                                      status=str(item.get("status") or ""))
                         for item in items if isinstance(item, dict))
            if len(rows) != len(items):
                raise IncompleteSnapshot("予定に不正な行があります")
            report = await asyncio.to_thread(sync_calendar, hub, CalendarSnapshot(source, complete, rows, expected_count),
                                             checked_at, days)
        except (IncompleteSnapshot, NotionError, ValueError, TypeError) as e:
            log.warning("%s を予定カレンダーに写せません: %s", source, e)
            return "error"
        return report.__dict__

    # 定期処理と研究テーマ

    def schedule_detail(self, name: str, day: str) -> dict:
        """その定期処理の、その日の記録（run_schedule が返した辞書）。まだなら空。"""
        row = self._assistant.store.last_schedule(name)
        if row is None or row["day"] != day:
            return {}
        return json.loads(row["detail"] or "{}") or {}

    def themes(self) -> list[Theme]:
        """研究テーマ（作業用のフォルダと、CLAUDE.md の検索キーワード・前提）。"""
        found = []
        for name, path in themes.all_themes(self._assistant.config).items():
            claude_md = path / "CLAUDE.md"
            premises = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
            found.append(Theme(name, path, tuple(themes.search_keywords(claude_md)), premises))
        return found

    async def to_thread(self, func, /, *args):
        """時間のかかる読み書き（Notion など）を、ほかの処理を止めずに動かす。"""
        return await asyncio.to_thread(func, *args)
