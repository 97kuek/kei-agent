"""モジュールがコアとやり取りする窓口（枠の版 1。書き方は docs/modules.md の「module.py」）。

モジュールの Python（modules/<名前>/module.py）が読み込んでよい Kei Agent の部品は、この kei_agent.api だけ
（同じフォルダのファイルは `from . import texts` のように読める）。ここに無いものに頼ると、コアを直したときに
動かなくなる。枠を変えるときは modules.API_VERSION を上げる。

module.py には `class Module` を置き、`__init__(self, core)` で窓口（Core）を受け取る。使う差し込み口だけを書く。

- `async on_message(req, skill="", params=None)` … モジュールのチャンネル・会話を受け持つ本体のチャンネル
  （module.toml の core_channels。Kei Agent のチャンネルなど）と、claim_thread したスレッドへの依頼者の書き込み。
  研究全体のチャンネルから回ってきたときは、振り分け係が選んだ仕事が skill と params に入る（空なら
  core.pick_skill で選べる）。答えは core.reply・core.converse・core.work で返す（どれも依頼の 👀 を ✅ に
  変える。例外を投げたら ⚠️ と知らせ）。[channels] か core_channels があれば必須
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
  （声なら喋る）。投げっぱなしなので、返事は要らない。出来事の種類は docs/modules.md の「module.py」の出来事の表
- `home() -> list[dict]` … App Home に出す、このモジュールの項目（Slack の blocks。見出しは本体が付ける）。
  押せるものの action_id は core.home_action_id(名前) で作る（チェックなら core.home_checkboxes）
- `async on_home_action(name, action)` … App Home の、このモジュールの項目が押されたとき（name は
  home_action_id に渡した名前、action は Slack の action）。依頼者のときだけ呼ばれ、終わると App Home を作り直す
- `async on_slash_command(name, body) -> str` … module.toml の [slash_commands] のコマンドが打たれたとき（body は Slack の
  command の中身）。返した文を、打った人にだけ見せる。Slack は3秒以内の返事を求めるので、時間のかかることは core.spawn に回す
- `async on_action(name, body)` … このモジュールの投稿のボタンなど（action_id は core.action_id(名前) で作る）が押されたとき
- `async on_view(name, body) -> dict | None` … このモジュールの入力の画面（callback_id は core.view_id(名前)）が送られたとき。
  欄の下に出す理由を {block_id: 文} で返すと、画面を閉じない
- `async material(now) -> list[str]` … Daily と振り返りの材料に足す行（今週の時間など）
- `async on_start()` … 起動して Slack につながったあと（Kei Agent を入れ替えたあとの起動なら、その結果は
  core.last_update() で受け取れる。途中で止まった作業の後始末など）
- `welcome() -> str` … モジュールのチャンネル（と core_channels の本体のチャンネル）に招かれたときの案内（できること）
- `default_question` … 本文の無いメンションのときに、担当に聞くこと

依頼者だけが押せる・打てる（ボタン・画面・コマンドは、本体が依頼者か確かめてから渡す）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent import (
    agents,
    briefing,
    dates,
    deadline,
    digest,
    guard,
    home,
    modules,
    one_shot,
    router,
    settings,
    themes,
)
from kei_agent.agents import Reply
from kei_agent.auto_messages import history_prompt
from kei_agent.briefing import Morning
from kei_agent.calendar_sync import JST, CalendarItem, CalendarSnapshot, IncompleteSnapshot, sync_calendar
from kei_agent.notion import NotionError
from kei_agent.one_shot import AIError
from kei_agent.records import Records
from kei_agent.request import Request
from kei_agent.response_output import (
    OutputError,
    finalize_conversation,
    safe_failure,
    validate_sections,
    validate_structured_response,
)
from kei_agent.slack_text import FAILED_PREFIX, escape, split_text
from kei_agent.theme_files import append_thread_log
from kei_agent.timelog import Toggl, TogglAmbiguousWrite, TogglError, load_toggl
from kei_agent.updates import Update

if TYPE_CHECKING:
    from kei_agent.assistant import Assistant

log = logging.getLogger(__name__)
API_VERSION = modules.API_VERSION
__all__ = ["API_VERSION", "ASK", "DIGEST_CHARS", "FAILED_PREFIX", "AIError", "Core", "Morning", "NotionError",
           "Records", "Reply", "Request",
           "Theme", "Toggl", "TogglAmbiguousWrite", "TogglError", "Update", "checked_sections", "checked_text",
           "contains_secret",
           "day_label", "due_clock", "due_day", "escape", "failure_text", "final_answer", "load_toggl", "parse_time",
           "selected_values", "theme_name", "weekday"]
# モジュールの投稿のボタンと入力の画面の名前の頭（本体が、どのモジュールのものかを見分ける）
MODULE_PREFIX = modules.ACTION_PREFIX
# Daily・振り返りの材料（core.digest）の上限の字数
DIGEST_CHARS = digest.MAX_DIGEST_CHARS
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


def theme_name(channel_name: str) -> str:
    """チャンネルの Slack での名前（`10_amr-query`）から、テーマの名前（並び順の番号を外した `amr-query`）。"""
    return themes.theme_name(channel_name)


def selected_values(action: dict) -> set[str]:
    """App Home で押された action の、選ばれている値（チェックなら付いているもの全部、選ぶ形なら1つ）。"""
    chosen = {str(option.get("value")) for option in action.get("selected_options") or []}
    if action.get("selected_option"):
        chosen.add(str(action["selected_option"].get("value")))
    return chosen


def reason_head(text: str) -> str:
    """担当が返した理由の頭（最初の「: 」まで。HTTP の状態があれば添える）。改善のチャンネルの1行に使う。"""
    head = re.split(r": |：", text.strip(), maxsplit=1)[0] or "理由なし"
    status = re.search(r"HTTP Error (\d{3})", text)
    return f"{head}、HTTP {status.group(1)}" if status else head


def final_answer(text: str) -> str:
    """AI の答え（core.run_ai が返す本文）のうち、Slack に出す部分（最終回答の印の中）。形が合わなければ空文字。"""
    try:
        return finalize_conversation(text)
    except OutputError:
        return ""


def contains_secret(text: str) -> bool:
    """秘密情報らしい文字列（鍵やトークンの形）を含むか。公開の場所（GitHub など）に書く前に確かめる。"""
    return any(pattern.search(text) for pattern in guard.SECRET_PATTERNS)


def checked_sections(text: str, headings: tuple[str, ...]) -> str | None:
    """AI の答え（Slack に出す部分）が、決まった見出し（`**今日のタスク**` など）をこの順で1回ずつ持ち、どれも中身が
    あるか確かめる。見出しの書き方はそろえて返す。形が違うもの・手元のパスや作業の実況を含むものは None。"""
    try:
        return validate_sections(text, headings)
    except OutputError:
        return None


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
    def provider(self) -> str:
        """App Home で選んだ、このモジュールの実行役の provider（claude / codex）。選ばれていないか、実行役が無ければ空文字。"""
        if self.spec.actor is None:
            return ""
        return settings.selected_provider(self._assistant.config, self._assistant.store, self.name)

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
        """その種類のチャンネルの名前（番号を外した名前。設定の [channels] で変えたものも）。module.toml の [channels] の
        種類と、本体のチャンネル（overview は研究全体、improve は Kei Agent のチャンネル）。"""
        config = self._assistant.config
        core = {"overview": config.overview_channels, "improve": config.improve_channels}
        return core[kind] if kind in core else config.module_channels.get(kind, ())

    async def post(self, channel: str, text: str, *, thread_ts: str | None = None,
                   blocks: list[dict] | None = None, markdown: bool = False) -> str:
        """投稿する（リンクのプレビューは付けない）。blocks を渡すとボタンなども置ける。markdown にすると、太字や
        箇条書きを Markdown で書ける（blocks とはいっしょに使えない）。投稿の ts を返す。"""
        where: dict = {"thread_ts": thread_ts} if thread_ts else {}
        if blocks is not None:
            where["blocks"] = blocks
        where["markdown_text" if markdown else "text"] = text
        posted = await self._assistant.slack.chat_postMessage(channel=channel, unfurl_links=False,
                                                              unfurl_media=False, **where)
        return str(posted.get("ts") or "")

    async def upload(self, channel: str, thread_ts: str, filename: str, content: str) -> None:
        """スレッドにファイルを添付する（差分などの文を、そのままファイルにして）。"""
        await self._assistant.slack.files_upload_v2(
            channel=channel, thread_ts=thread_ts,
            file_uploads=[{"filename": filename, "title": filename, "content": content}])

    async def thread_messages(self, channel: str, thread_ts: str) -> list[dict]:
        """スレッドの投稿（Slack の message。古い順。長いスレッドは、新しいほうから決まった件数まで）。"""
        messages, _ = await self._assistant.thread_messages(channel, thread_ts)
        return messages

    async def thread_history(self, channel: str, thread_ts: str) -> str:
        """スレッドのやりとりを、AI に渡す文にしたもの（依頼者と Kei Agent の発言を順に。省いた古い投稿も書き添える）。"""
        messages, dropped = await self._assistant.thread_messages(channel, thread_ts)
        return history_prompt(messages, self._assistant.bot_user_id, "", None, dropped=dropped)

    @asynccontextmanager
    async def progress(self, req: Request, text: str):
        """その間、スレッドの入力欄の下に経過（text。「取り込み中…」など）を出す。"""
        ui = self._assistant.thread_ui(req)
        await ui.start()
        await ui.show(text)
        try:
            yield
        finally:
            await ui.finish("")

    async def update(self, channel: str, ts: str, text: str, *, blocks: list[dict] | None = None) -> None:
        """自分の投稿を書き換える（消されていたら Slack の例外がそのまま上がる）。"""
        await self._assistant.slack.chat_update(channel=channel, ts=ts, text=text,
                                                **({"blocks": blocks} if blocks is not None else {}))

    async def open_view(self, trigger_id: str, view: dict) -> dict:
        """入力の画面を開く（trigger_id は押されてから3秒で切れる）。開いた画面（id など）を返す。"""
        opened = await self._assistant.slack.views_open(trigger_id=trigger_id, view=view)
        return dict(opened.get("view") or {})

    async def update_view(self, view_id: str, view: dict) -> None:
        """開いている画面を差し替える（読み込み中の画面を、あとから中身に替えるときなど）。"""
        await self._assistant.slack.views_update(view_id=view_id, view=view)

    async def permalink(self, channel: str, ts: str) -> str:
        """投稿へのリンク。"""
        return await self._assistant.permalink(channel, ts)

    async def channel_name(self, channel: str) -> str:
        """チャンネルの Slack での名前（番号つき。`20_course` など）。"""
        info = await self._assistant.slack.conversations_info(channel=channel)
        return str(info["channel"]["name"])

    def action_id(self, name: str) -> str:
        """このモジュールの投稿に置くボタンなどの action_id（押されると on_action(name, body)）。"""
        return modules.action_id(self.name, name)

    def view_id(self, name: str) -> str:
        """このモジュールの入力の画面の callback_id（送られると on_view(name, body)）。"""
        return modules.action_id(self.name, name)

    def spawn(self, coro) -> None:
        """裏で動かす（Slack に3秒以内に返したあとに、Toggl や Notion に送るときなど）。落ちたらログに残る。"""
        self._assistant.spawn(coro)

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
        # AI の実行役（[actor]）を持たないモジュールの担当は、AI を選ばない（provider を渡さない）
        provider = (settings.selected_provider(self._assistant.config, self._assistant.store, self.name)
                    if self.spec.actor is not None else "")

        async def keep_alive(_status: str) -> None:
            # AI を動かす仕事なので、経過を流しながら受け取る（途中で切られないように）
            return None

        reply = await agents.ask(agent, skill, params={"provider": provider} if provider else {},
                                 on_progress=keep_alive, text=json.dumps(payload, ensure_ascii=False))
        if not reply.ok:
            # 改善のチャンネルには最初の「: 」の前だけが出るので、理由の頭はそこに入れる（全文はログに残る）
            await self.notify_trouble(f"{self.spec.label}の担当の {skill} がうまくいかなかった（{reason_head(reply.text)}）: "
                                      f"{agent.base_url} が返した理由: {reply.text[:300]}")
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

    async def ask_module(self, name: str, skill: str, payload: dict) -> Reply:
        """ほかのモジュールの担当プロセスに仕事を頼む（module.toml の [depends] に書いた相手だけ）。"""
        if name not in (*self.spec.requires, *self.spec.optional):
            raise ValueError(f"モジュール「{self.name}」の [depends] に {name} がありません")
        core = self._assistant.cores.get(name)
        if core is None:
            return Reply.broken(f"モジュール「{name}」はオンになっていません")
        return await core.ask_agent(skill, payload)

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

    async def work(self, req: Request, *, folder: Path | None = None, hide: tuple[str, ...] = ()) -> str:
        """チャンネルの作業場を使って、このモジュールの担当と会話して答える。

        研究テーマのチャンネル（[channels] に "*" で受け持つもの）は、テーマのフォルダが作業場。folder（このモジュールの
        フォルダ core.state_dir の中）を渡すと、会話を受け持つほかのチャンネル（モジュールのチャンネル、core_channels の
        本体のチャンネル）で、そのフォルダを作業場にする。hide に書いた頭で始まる行は Slack に出さない（合図の行など）。
        添付の保存・できたファイルの添付・接続先の許可・引き継ぎの提案・ジョブは、研究と同じ流れ。担当のプロセスには
        ask の依頼に channel_name（作業場）と allowed_domains（許可済みの接続先）が添えて届く（プロセスの無いモジュールは
        本体の中で動かす）。返すのは AI の答えのうち Slack に出す部分（hide の行も含む。答えられなかったら空文字）。
        """
        result = await self._assistant.work_in_workspace(
            req, self.name, folder=self._inside(folder) if folder is not None else None, hide=tuple(hide))
        return final_answer(result.text) if result is not None and not result.is_error else ""

    async def run_ai(self, use_case: str, prompt: str, *, folder: Path | None = None, req: Request | None = None,
                     status: str = "", overview: bool = False, trigger: str = "") -> str:
        """このモジュールの実行役の用途で AI を1回動かし、答えの本文を返す（会話にはしない）。動かせなければ AIError。

        folder を渡さなければ、作業場を読むだけで動かす（要約などの係）。folder（このモジュールのフォルダ core.state_dir の
        中）を渡すと、そこで動かし、書き込みもそこだけ（書けるかどうかは [actor] の files が決める）。overview にすると、
        研究全体の作業場で読むだけで動かす（研究テーマのフォルダとスレッドの記録を全部読める。Daily・振り返り）。
        req を渡すと、そのスレッドに経過（最初は status）を出し、終わったら答えのうち Slack に出す部分（final_answer）を
        見せて、依頼者の返事を待つ形にする。返すのは AI の答えの本文そのまま（JSON を読む係などのため）。
        動いている間は、Kei Agent の入れ替え（再起動）を待たせる。動かした時間は Kei Agent の稼働として記録し
        （trigger はその見出し。App Home の最近の動きに出る）、上限に当たったら明けるまで定期処理を止める。
        """
        assistant = self._assistant
        config = assistant.config
        target = workspace = None
        if folder is not None:
            target = self._inside(folder)
            target.mkdir(parents=True, exist_ok=True)
        where = req.channel_name if req is not None else self.name
        if overview:
            where = config.overview_channels[0]
            workspace = replace(themes.resolve(config, where), module=self.name)
            themes.ensure_workspace(workspace)
        ui = assistant.thread_ui(req) if req is not None else None
        if ui is not None:
            await ui.start()
            if status:
                await ui.show(status)
        run_id = assistant.store.start_run(req.channel if req is not None else "",
                                           req.thread_ts if req is not None else "", where, trigger or self.name)
        result = None
        try:
            with assistant.claude_running():
                result = await one_shot.run_result(
                    config, assistant.store, self.name, use_case, prompt, folder=target, workspace=workspace,
                    channel=req.channel if req is not None else "", thread_ts=req.thread_ts if req is not None else "",
                    on_activity=ui.activity if ui is not None else None)
        finally:
            assistant.store.end_run(run_id, result is None or result.is_error, result.cost_usd if result else None)
            if ui is not None and (result is None or result.is_error):
                with suppress(Exception):
                    await ui.finish("")
        if result.limit_reset_at is not None and result.provider:
            # 上限に当たった。明けるまで、その provider の定期処理を始めない
            assistant.store.set_limit_until(result.provider, max(assistant.store.limit_until(result.provider),
                                                                 assistant.limit_until(result.limit_reset_at)))
        await assistant.tell_failure(self.name, result)
        if result.is_error:
            raise AIError(result.failure_reason(), result.limit_reset_at)
        if ui is not None:
            await ui.finish(final_answer(result.text) or safe_failure("conversation"), awaiting=True)
        return result.text

    async def converse(self, req: Request) -> None:
        """そのスレッドの会話として、このモジュールの担当（[actor] と [process]）に聞いて答える。

        会話の続き・経過の表示・上限に当たったときのやり直し・出力の確認は、大学や仕事の担当と同じ。
        """
        await self._assistant.converse_with_agent(req, self.name)

    # Daily・振り返り（本体の定期処理を受け持つモジュール。module.toml の core_schedules）

    async def morning(self, now: datetime) -> Morning:
        """朝の一覧（今日の予定を時刻順に1通。Slack にそのまま出せる形）。

        集めるのは、モジュールの取り込み（prepare）・予定（agenda）・朝の一覧の行（morning_notes）と、前回の Daily から
        うまくいかなかった定期処理。会議は出典ごとに共通ホームの予定カレンダーにも写し、声には1週間ぶんの予定を
        出来事（schedule）で渡す。Slack に出せたら mark_shown(notices) を呼ぶ。
        """
        return await briefing.build(self._assistant, now, skip=self.name)

    def mark_shown(self, notices) -> None:
        """朝の一覧を Slack に出せたあとに呼ぶ（出した締切を、24時間前の知らせで繰り返さないための目印を残す）。"""
        for key in notices:
            self._assistant.store.record_notice(key)

    async def gather_prepare(self, kind: str, day: str) -> list[str]:
        """モジュールに取り込み直してもらう（class Module の prepare。kind は daily / review）。
        うまくいかなかったことの短い名前を返す。"""
        return await self._assistant.module_prepare(kind, day)

    async def gather_agenda(self, days: int, kinds=None) -> tuple[dict[str, list[dict]], list[str]]:
        """モジュールの予定（class Module の agenda）。読めたモジュールの名前 → 予定と、読めなかったモジュールの表示名。"""
        return await self._assistant.module_agenda(days, frozenset(kinds) if kinds is not None else None)

    async def digest(self, since: float, now: float, title: str, *, agenda: bool = False) -> str:
        """Daily・振り返りの材料（DIGEST_CHARS 字までで、超えた分は後ろのノートから省いたもの）。

        本体の記録（やり取りのあったスレッドとその記録の場所・終わったジョブ・夜間の Task・止まっているテーマ・
        返事待ち・Kei Agent の稼働）、モジュールの材料（material）、研究ホームの Task とノート、前日の振り返り。
        agenda にすると、モジュールの予定（今日あったもの・明日のもの・締切）も入れる。
        """
        ids = await self._assistant.channel_ids()
        return await digest.DigestBuilder(self._assistant.config, self._assistant.store, self._assistant).build(
            since, now, title, set(ids), domains=agenda, skip=self.name)

    async def publish(self, channel: str, header: str, text: str) -> str:
        """チャンネルに見出しを出し、そのスレッドに本文（Markdown。長ければ分ける）を出す。スレッドの ts を返す。

        スレッドへの返信は、そのチャンネルの担当が続ける（研究全体のチャンネルなら、研究テーマを受け持つモジュール）。
        本文はスレッドの記録にも残す（続きの会話で読めるように）。
        """
        assistant = self._assistant
        posted = await assistant.slack.chat_postMessage(channel=channel, text=header)
        thread_ts = str(posted["ts"])
        name = await assistant.channel_name(channel)
        # 返信を拾えるように、スレッドを覚えておく
        assistant.store.upsert_thread(channel, thread_ts, name, None)
        if text.strip():
            with suppress(ValueError):
                ws = themes.resolve(assistant.config, name)
                if ws.cwd is not None:
                    append_thread_log(ws.cwd, name, thread_ts, "Kei Agent", text)
            for chunk in split_text(text):
                await assistant.slack.chat_postMessage(channel=channel, thread_ts=thread_ts, markdown_text=chunk)
        return thread_ts

    def collect_conclusions(self, channel: str, thread_ts: str, page_id: str) -> None:
        """そのスレッド（振り返り）に依頼者が貼った結論を、共通ホームの日別記録のページに書き足すようにする。"""
        self._assistant.store.link_notion(channel, thread_ts, page_id, "review")

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

    # 自分のフォルダと、Kei Agent 自身

    @property
    def state_dir(self) -> Path:
        """このモジュールが持つファイルの置き場（状態の置き場の modules/<名前>。無ければ作る）。"""
        path = self._assistant.config.module_state(self.name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _inside(self, folder: Path) -> Path:
        """AI に使わせるフォルダが、このモジュールのフォルダの中か確かめる（外なら ValueError）。"""
        resolved = Path(folder).expanduser().resolve()
        if not resolved.is_relative_to(self.state_dir.resolve()):
            raise ValueError(f"モジュール「{self.name}」のフォルダ（core.state_dir）の外では、AI を動かせません: {folder}")
        return resolved

    @property
    def repo_root(self) -> Path:
        """Kei Agent 自身のコードの置き場（Git のリポジトリ）。自分を直すモジュール（自己改善）が使う。"""
        return self._assistant.config.repo_root

    async def check_change(self, folder: Path, base: str) -> list[str]:
        """Kei Agent 自身を直した差分（folder の Git の、base から HEAD まで）を、本体の柵で確かめる。

        柵のファイル（guard.py・config.example.toml・deploy/）に触れていないか、秘密情報らしいものや大きすぎる差分が
        無いか。問題の説明を返す（無ければ空。柵を変えたいときは人が直す）。
        """
        return await asyncio.to_thread(guard.check_change, Path(folder), base, "HEAD")

    @contextmanager
    def busy(self):
        """その間は、Kei Agent の入れ替え（再起動）を待たせる（途中で止まると困る、外への書き込みなど）。"""
        with self._assistant.claude_running():
            yield

    def restart_for_update(self, previous: str, note: str = "") -> None:
        """Kei Agent を新しい版で起動し直す（動いている AI の作業が終わってから。ほかのプロセスも一緒に）。

        previous は取り込む前のコミット。新しい版が Slack につながらないまま起動を繰り返したら、本体（deploy/run.sh）が
        そこまで戻す。note（1行）は、次に起動したときに core.last_update() で受け取れる（どのスレッドの取り込みか、など）。
        """
        self._assistant.restart_for_update(previous, note)

    def last_update(self) -> Update | None:
        """この起動が、core.restart_for_update で入れ替えたあとのものなら、その結果（無ければ None）。

        Update の state は done（新しい版で動いた）か rolled_back（起動できず、previous に戻した）。on_start で読む。
        """
        return self._assistant.last_update

    # 定期処理と研究テーマ

    def last_ran(self, name: str, before_day: str = "") -> float | None:
        """その定期処理が最後に動いた時刻（before_day を渡せば、その日より前の日の分から）。まだなら None。"""
        row = self._assistant.store.last_schedule(name, before_day=before_day or None)
        return float(row["ran_at"]) if row is not None else None

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
