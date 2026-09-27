"""会社のアカウントに付いている Microsoft 365 の連携から、Outlook の予定を機械の形で読む。

会社の IT が Anthropic・OpenAI のアプリに読み取りを許可しているので、Entra ID にアプリを登録しなくても
Outlook を読める。この連携はアカウント側にあり、Python からは直接呼べない。そこで選択済み provider を
「読むだけ」の1回として動かし（窓口の run_ai）、JSON で答えさせる。

- 使える道具は module.toml の [[actor.connectors]] が決める。送信・作成・削除はできない
- `list-events` が返すのは件名・時間・場所・主催者・リンクまで。朝のまとめで1行ずつ並べる形なので、
  本文を入れても読めない（秘密のためではない。自由な質問では本文を出してよい。work.md）
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from kei_agent_a2a.api import AIError, Config, json_list, run_ai

log = logging.getLogger(__name__)

AGENT = "work"
DEFAULT_DAYS = 7
# 予定を読む回の用途（module.toml の [use_cases]）
USE_CASE = "work_single_source"

PROMPT = """Outlook の予定を検索する道具を使って、{since} から {until} までの私の予定を調べてください。

見つかった予定を、JSON の配列だけで答えてください。前置きも説明も書かないでください。
配列の1つは次の形です（値が無ければ空文字）。

[{{"subject": "件名", "start": "2026-09-24T18:00", "end": "2026-09-24T19:00",
   "id": "Outlook の予定 ID", "location": "場所", "organizer": "主催者のメールアドレス",
   "all_day": false, "url": "Outlook の予定ページのリンク"}}]

- 時刻は Tokyo Standard Time の壁時計の時刻をそのまま使い、分までにしてください
- 取り消された予定は除いてください
- 予定が無ければ [] とだけ答えてください
- 本文（会議の詳細、Teams の参加リンク、パスコード）は入れないでください"""


class WorkCalendarError(RuntimeError):
    def __init__(self, message: str, limit_reset_at: float | None = None):
        super().__init__(message)
        self.limit_reset_at = limit_reset_at


async def _read(config: Config, store, prompt: str, provider: str = "") -> str:
    """予定を JSON で答えさせる1回（読むだけ。会話は続けない）。"""
    try:
        return await run_ai(config, store, AGENT, USE_CASE, prompt, provider=provider)
    except AIError as e:
        raise WorkCalendarError(f"Outlook の予定を読めませんでした: {e}", e.limit_reset_at) from None


async def events(config: Config, store, days: int = DEFAULT_DAYS, today: date | None = None,
                 provider: str = "") -> list[dict]:
    """これから days 日ぶんの予定を、始まる順に。"""
    start = today or date.today()
    prompt = PROMPT.format(since=start.isoformat(), until=(start + timedelta(days=max(days, 1))).isoformat())
    text = await _read(config, store, prompt, provider)
    try:
        found = json_list(text)
    except ValueError as e:
        raise WorkCalendarError(str(e)) from None
    events_ = [_event(item) for item in found if str(item.get("start") or "").strip()]
    events_.sort(key=lambda e: e["start"])
    log.info("Outlook の予定を %d 件読みました（%d 日ぶん）", len(events_), days)
    return events_


def _event(item: dict) -> dict:
    return {
        "id": str(item.get("id") or ""),
        "subject": str(item.get("subject") or "（件名なし）"),
        "start": str(item.get("start") or "")[:16],
        "end": str(item.get("end") or "")[:16],
        "all_day": bool(item.get("all_day")),
        "location": str(item.get("location") or ""),
        "organizer": str(item.get("organizer") or ""),
        "free": False,
        "url": str(item.get("url") or ""),
    }
