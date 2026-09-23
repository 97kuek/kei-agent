"""会社の Claude アカウントに付いている Microsoft 365 の連携から、Outlook を読む。

会社の IT が Anthropic のアプリに読み取りを許可しているので（`Calendars.Read` など）、
Entra ID にアプリを登録しなくても Outlook を読める。

この連携は Claude のアカウント側にあり、Python からは直接呼べない。そこで claude を
**連携の道具1つだけ**に絞って動かし、JSON で答えさせる（`kei_agent_a2a.claude.ask_connector`）。

- 使わせるのは `outlook_calendar_search`（読むだけ）。送信・作成・削除の道具は名指しで断る
- 会社の契約枠で動かすため、仕事用の秘密情報ファイルに会社の `CLAUDE_CODE_OAUTH_TOKEN` を置く
- `list-events` が返すのは件名・時間・場所・主催者・リンクまで。朝のまとめで1行ずつ並べる形なので、
  本文を入れても読めない（秘密のためではない。自由な質問では本文を出してよい。prompts/work.md）
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from kei_agent.config import Config
from kei_agent.store import Store
from kei_agent_a2a import claude

log = logging.getLogger(__name__)

AGENT = "work"
# 使わせる連携の道具（読むだけ）
_M365 = "mcp__claude_ai_Microsoft_365__"
CALENDAR_TOOL = f"{_M365}outlook_calendar_search"
# 予定を機械の形で取るときは、この1つだけ
ALLOWED = (CALENDAR_TOOL,)
# 自由な質問に答えるとき。読むものだけを並べる（送信・作成・削除は入れない）
ALLOWED_ASK = (
    CALENDAR_TOOL,
    f"{_M365}outlook_email_search",
    f"{_M365}search_people",
    f"{_M365}find_meeting_availability",
    # 見つけたものの本文を読む（読んで要約するため。貼り付けは prompts/work.md で止める）
    f"{_M365}read_resource",
)
# 名指しで断る道具（許可の一覧に入れていなくても、念のため）
DENY = (
    "mcp__claude_ai_Microsoft_365__outlook_send_mail",
    "mcp__claude_ai_Microsoft_365__outlook_send_draft",
    "mcp__claude_ai_Microsoft_365__outlook_create_event",
    "mcp__claude_ai_Microsoft_365__outlook_update_event",
    "mcp__claude_ai_Microsoft_365__outlook_delete_event",
    "mcp__claude_ai_Microsoft_365__teams_send_chat_message",
    "mcp__claude_ai_Microsoft_365__teams_send_channel_message",
    "mcp__claude_ai_Microsoft_365__sharepoint_upload_file",
)
DEFAULT_DAYS = 7
TIMEOUT_MINUTES = 3
# 自由な質問は、探して読んで要約するので、少し長めに待つ
ASK_TIMEOUT_MINUTES = 5

PROMPT = """{tool} を使って、{since} から {until} までの私の予定を調べてください。

見つかった予定を、JSON の配列だけで答えてください。前置きも説明も書かないでください。
配列の1つは次の形です（値が無ければ空文字）。

[{{"subject": "件名", "start": "2026-09-24T18:00", "end": "2026-09-24T19:00",
   "location": "場所", "organizer": "主催者のメールアドレス", "all_day": false, "url": "Outlook のリンク"}}]

- 時刻は Tokyo Standard Time の壁時計の時刻をそのまま使い、分までにしてください
- 取り消された予定は除いてください
- 予定が無ければ [] とだけ答えてください
- 本文（会議の詳細、Teams の参加リンク、パスコード）は入れないでください"""


class WorkCalendarError(RuntimeError):
    pass


async def events(config: Config, days: int = DEFAULT_DAYS, today: date | None = None,
                 store: Store | None = None) -> list[dict]:
    """これから days 日ぶんの予定を、始まる順に。"""
    start = today or date.today()
    prompt = PROMPT.format(tool=CALENDAR_TOOL, since=start.isoformat(),
                           until=(start + timedelta(days=max(days, 1))).isoformat())
    try:
        text = await claude.ask_connector(config, prompt, ALLOWED, config.agent_plugin_dir(AGENT),
                                          DENY, TIMEOUT_MINUTES, store=store or Store(config.db_path), agent=AGENT)
        found = claude.json_reply(text)
    except claude.ConnectorError as e:
        raise WorkCalendarError(str(e)) from None
    events_ = [_event(item) for item in found if str(item.get("start") or "").strip()]
    events_.sort(key=lambda e: e["start"])
    log.info("Outlook の予定を %d 件読みました（%d 日ぶん）", len(events_), days)
    return events_


def _event(item: dict) -> dict:
    return {
        "subject": str(item.get("subject") or "（件名なし）"),
        "start": str(item.get("start") or "")[:16],
        "end": str(item.get("end") or "")[:16],
        "all_day": bool(item.get("all_day")),
        "location": str(item.get("location") or ""),
        "organizer": str(item.get("organizer") or ""),
        "free": False,
        "url": str(item.get("url") or ""),
    }


async def ask(config: Config, question: str, prompt_path: Path | None = None, store: Store | None = None) -> str:
    """自由な質問に、連携を読んで答える（長さの加減は prompts/work.md が決める）。"""
    guide = (prompt_path or config.repo_root / "prompts" / "work.md")
    instructions = guide.read_text(encoding="utf-8") if guide.exists() else ""
    prompt = f"{instructions}\n\n---\n\n今日は {date.today().isoformat()}。次の質問に答えてください。\n\n{question}"
    from kei_agent.model_classifier import UsageLimited, classify_work
    actual_store = store or Store(config.db_path)
    try:
        use_case = await classify_work(config, actual_store, question)
    except UsageLimited as e:
        raise WorkCalendarError(str(e)) from None
    try:
        return await claude.ask_connector(config, prompt, ALLOWED_ASK, config.agent_plugin_dir(AGENT),
                                          DENY, ASK_TIMEOUT_MINUTES, store=actual_store, agent=AGENT,
                                          use_case=use_case)
    except claude.ConnectorError as e:
        raise WorkCalendarError(str(e)) from None
