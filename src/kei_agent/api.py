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
- `morning_notes(day) -> list[str]` … 朝の一覧（Daily の投稿）に足す行
- `async agenda(days) -> list[dict] | None` … これから days 日の、時刻のある予定。朝の一覧・声のレイヤ・
  共通ホームの予定カレンダー（source ごと）・振り返りの材料に載る。1件は
  `{"kind": "meeting", "subject", "start": "YYYY-MM-DDTHH:MM", "end", "location", "url", "id", "source"}`。
  読めなかったら None を返す（空の [] と分ける。予定カレンダーの行を「要確認」にしないため）
- `welcome() -> str` … モジュールのチャンネルに招かれたときの案内（できること）
- `default_question` … 本文の無いメンションのときに、担当に聞くこと
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent import agents, dates, modules, router, settings, themes
from kei_agent.agents import Reply
from kei_agent.notion import NotionError
from kei_agent.request import Request
from kei_agent.response_output import safe_failure
from kei_agent.slack_text import escape, split_text

if TYPE_CHECKING:
    from kei_agent.assistant import Assistant

API_VERSION = modules.API_VERSION
__all__ = ["API_VERSION", "ASK", "Core", "NotionError", "Records", "Reply", "Request", "Theme", "day_label", "escape",
           "failure_text"]
# 定型に当てはまらない質問の窓口（どの担当の名刺でも同じ名前）
ASK = router.ASK
_KEEP = object()


def failure_text(kind: str = "connection") -> str:
    """失敗したときに Slack に出す、決まった文（connection / timeout / login など。中の詳しいことは書かない）。"""
    return safe_failure(kind)


def day_label(day: str | date) -> str:
    """9/25（金）の形。YYYY-MM-DD の文字列も渡せる。"""
    return dates.day_label(date.fromisoformat(day) if isinstance(day, str) else day)


@dataclass(frozen=True)
class Theme:
    """研究テーマ（研究がモジュールになるまでは、コアが持つ）。"""
    name: str
    path: Path
    # テーマの CLAUDE.md の「## 検索キーワード」と、CLAUDE.md の本文（前提）
    keywords: tuple[str, ...]
    premises: str


class Records:
    """そのモジュールだけの記録。種類と鍵で1件、中身は JSON にできる辞書。

    keep_days を付けたものは、その日数を過ぎると毎晩の保守で消える（付けなければ、消すまで残る）。
    """

    def __init__(self, store, module: str):
        self._store = store
        self._module = module

    def put(self, kind: str, key: str, value: dict, *, keep_days: float | None = None) -> None:
        self._store.put_module_record(self._module, kind, key, json.dumps(value, ensure_ascii=False),
                                      _expires(keep_days))

    def get(self, kind: str, key: str) -> dict | None:
        row = self._store.module_record(self._module, kind, key)
        return json.loads(row["value"]) if row is not None else None

    def update(self, kind: str, key: str, *, keep_days: float | None | object = _KEEP, **changes) -> dict | None:
        """中身の一部を書き換える。keep_days を渡せば、残す日数も変える（None なら消すまで残す）。無ければ None。"""
        row = self._store.module_record(self._module, kind, key)
        if row is None:
            return None
        value = {**json.loads(row["value"]), **changes}
        expires = row["expires_at"] if keep_days is _KEEP else _expires(keep_days)
        self._store.put_module_record(self._module, kind, key, json.dumps(value, ensure_ascii=False), expires)
        return value

    def items(self, kind: str) -> list[dict]:
        """その種類の記録の中身（新しく書いた順）。"""
        return [json.loads(row["value"]) for row in self._store.module_records(self._module, kind)]

    def delete(self, kind: str, key: str) -> None:
        self._store.delete_module_record(self._module, kind, key)


def _expires(keep_days: float | None) -> float | None:
    return None if keep_days is None else time.time() + keep_days * 86400


class Core:
    """1つのモジュールのための窓口。Slack・担当・記録・定期処理の記録・研究テーマに、決めた形でだけ触れる。"""

    def __init__(self, assistant: Assistant, spec: modules.ModuleSpec):
        self._assistant = assistant
        self.spec = spec
        self.name = spec.name
        self.records = Records(assistant.store, spec.name)

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

    async def converse(self, req: Request) -> None:
        """そのスレッドの会話として、このモジュールの担当（[actor] と [process]）に聞いて答える。

        会話の続き・経過の表示・上限に当たったときのやり直し・出力の確認は、大学や仕事の担当と同じ。
        """
        await self._assistant.converse_with_agent(req, self.name)

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
        for path in themes.theme_dirs(self._assistant.config):
            claude_md = path / "CLAUDE.md"
            premises = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
            found.append(Theme(path.name, path, tuple(themes.search_keywords(claude_md)), premises))
        return found

    async def to_thread(self, func, /, *args):
        """時間のかかる読み書き（Notion など）を、ほかの処理を止めずに動かす。"""
        return await asyncio.to_thread(func, *args)
