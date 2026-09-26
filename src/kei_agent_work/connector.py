"""会社のアカウントに付いている Microsoft 365 の連携から、Outlook の予定を機械の形で読む。

会社の IT が Anthropic・OpenAI のアプリに読み取りを許可しているので、Entra ID にアプリを登録しなくても
Outlook を読める。この連携はアカウント側にあり、Python からは直接呼べない。そこで選択済み provider を
共通の起動口（`kei_agent.runner`）で「読むだけ」の1回として動かし、JSON で答えさせる。

- 使える道具は制限の表（`kei_agent.agent_policy` の OUTLOOK）が決める。送信・作成・削除はできない
- `list-events` が返すのは件名・時間・場所・主催者・リンクまで。朝のまとめで1行ずつ並べる形なので、
  本文を入れても読めない（秘密のためではない。自由な質問では本文を出してよい。prompts/work.md）
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta

from kei_agent import runner, themes
from kei_agent.config import Config
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve, resolve_selected
from kei_agent.store import Store
from kei_agent_a2a import run

log = logging.getLogger(__name__)

AGENT = "work"
DEFAULT_DAYS = 7

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

SNAPSHOT_PROMPT = """Outlook の予定を検索する道具で、{since} から {until} までの予定を全件検索してください。
検索結果に続きがある場合は全ページを取得し、取得件数と検索元の件数を照合してください。
JSON オブジェクトだけ返してください。説明や Markdown は不要です。
{{"complete": true, "has_more": false, "source_count": 0, "items": []}}
items の各要素は id/subject/start/end/location/url/all_day だけを含めます。
id は予定の安定 ID。得られなければ空文字にしてください。
source_count が分からない、件数が一致しない、検索結果が途中で切れている場合は complete を false にしてください。
時刻は Tokyo Standard Time の壁時計の時刻。本文、Teams 参加リンク、パスコード、メール本文は絶対に含めません。"""


class WorkCalendarError(RuntimeError):
    def __init__(self, message: str, limit_reset_at: float | None = None):
        super().__init__(message)
        self.limit_reset_at = limit_reset_at


async def _read(config: Config, store: Store, prompt: str, provider: str = "") -> str:
    """予定を JSON で答えさせる1回（読むだけ。会話は続けない）。"""
    use_case = UseCase.WORK_SINGLE_SOURCE
    try:
        recipe = (resolve(AGENT, provider, use_case) if provider
                  else resolve_selected(config, store, AGENT, use_case))
    except ModelPolicyError as e:
        raise WorkCalendarError(str(e)) from None
    result = await runner.run_model(config, runner.ExecutionRequest(
        themes.agent_workspace(config, AGENT), recipe, None, "", "", read_only=True), prompt)
    if result.is_error:
        reason = "; ".join(result.errors)[:200] or result.failure_kind or "理由不明"
        raise WorkCalendarError(f"Outlook の予定を読めませんでした: {reason}", result.limit_reset_at)
    return result.text


async def events(config: Config, days: int = DEFAULT_DAYS, today: date | None = None,
                 store: Store | None = None, provider: str = "") -> list[dict]:
    """これから days 日ぶんの予定を、始まる順に。"""
    start = today or date.today()
    prompt = PROMPT.format(since=start.isoformat(), until=(start + timedelta(days=max(days, 1))).isoformat())
    text = await _read(config, store or Store(config.db_path), prompt, provider)
    try:
        found = run.json_reply(text)
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


async def calendar_snapshot(config: Config, days: int = 30, today: date | None = None,
                            store: Store | None = None, provider: str = "") -> dict:
    """完全性を機械検証できない取得は complete=False とする。"""
    start = today or date.today()
    prompt = SNAPSHOT_PROMPT.format(since=start.isoformat(),
                                    until=(start + timedelta(days=max(days, 1))).isoformat())
    try:
        text = await _read(config, store or Store(config.db_path), prompt, provider)
        # events() と同じく、```json の囲みや前置きが付いても読む
        raw = run.json_object(text, "items")
    except WorkCalendarError as e:
        raise WorkCalendarError(f"Outlook の完全な予定一覧を確認できません: {e}", e.limit_reset_at) from None
    except ValueError as e:
        raise WorkCalendarError(f"Outlook の完全な予定一覧を確認できません: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise WorkCalendarError("Outlook の予定一覧が正しい JSON オブジェクトではありません")
    source_count = raw.get("source_count")
    complete = (raw.get("complete") is True and raw.get("has_more") is False
                and type(source_count) is int and source_count == len(raw["items"])
                and all(isinstance(item, dict) for item in raw["items"]))
    items = []
    for raw_item in raw["items"]:
        if not isinstance(raw_item, dict):
            continue
        event = _event(raw_item)
        if not event["id"] and event["url"] and event["start"] and event["subject"]:
            fingerprint = "\0".join((event["url"], event["start"], event["subject"]))
            event["id"] = "fallback:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        items.append({key: event[key] for key in ("id", "subject", "start", "end",
                                                  "all_day", "location", "url")})
    if any(not item["start"] or not item["id"] for item in items):
        complete = False
    # 件数も complete も LLM の申告であり、検索 API 自身のページング証明ではない。
    # カレンダーへの自動書込は独立に検証できる取得経路ができるまで停止する。
    return {"complete": complete, "verified_complete": False,
            "source_count": source_count, "items": items}
