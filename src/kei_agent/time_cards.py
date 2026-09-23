"""Slack の固定時間記録カード。"""

from __future__ import annotations

import json
from datetime import datetime

from kei_agent.time_tracking import TimeEntry

START = "kei_agent_time_start"
STOP = "kei_agent_time_stop"
MEMO = "kei_agent_time_memo"
RETRY = "kei_agent_time_retry"
MEMO_CALLBACK = "kei_agent_time_memo_submit"
COURSE_CALLBACK = "kei_agent_time_course_submit"


def blocks(entry: TimeEntry | None = None) -> list[dict]:
    if entry is None:
        text, action, style, value = "⏱️ 時間を記録する", START, "primary", "start"
        label = "開始"
    else:
        started = datetime.fromtimestamp(entry.started_at).strftime("%H:%M")
        text, action, style, value = f"⏱️ 計測中 · {entry.description}\n開始: {started}", STOP, "danger", entry.id
        label = "停止"
    elements = [{"type": "button", "action_id": action, "style": style,
                 "text": {"type": "plain_text", "text": label}, "value": value}]
    if entry is not None:
        elements.append({"type": "button", "action_id": MEMO,
                         "text": {"type": "plain_text", "text": "メモを追加"}, "value": entry.id})
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": elements}]


def fallback_text(entry: TimeEntry | None = None) -> str:
    return "計測中" if entry is not None else "時間を記録する"


def retry_blocks(entry: TimeEntry) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn",
             "text": "⚠️ Toggl への送信結果を確認できません。Toggl 側を確認してから必要なときだけ再送してね。"}},
            {"type": "actions", "elements": [{"type": "button", "action_id": RETRY,
             "text": {"type": "plain_text", "text": "Togglへ再送"}, "value": entry.id}]}]


def memo_view(entry: TimeEntry) -> dict:
    return {
        "type": "modal", "callback_id": MEMO_CALLBACK, "private_metadata": entry.id,
        "title": {"type": "plain_text", "text": "メモを追加"},
        "submit": {"type": "plain_text", "text": "保存"},
        "close": {"type": "plain_text", "text": "閉じる"},
        "blocks": [{"type": "input", "block_id": "memo", "optional": True,
                    "label": {"type": "plain_text", "text": "作業メモ"},
                    "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                                "initial_value": entry.memo}}],
    }


def course_view(channel_id: str, courses: list[dict]) -> dict:
    """今学期の科目だけを選ばせ、選択後に時間計測を始める。"""
    options = []
    for item in courses[:100]:
        name = str(item.get("subject") or item.get("name") or "科目名未設定").strip()
        page_id = str(item.get("id") or item.get("page_id") or "")
        if page_id:
            options.append({"text": {"type": "plain_text", "text": name[:75]},
                            "value": json.dumps({"id": page_id, "name": name}, ensure_ascii=False)})
    return {
        "type": "modal", "callback_id": COURSE_CALLBACK, "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "科目を選ぶ"},
        "submit": {"type": "plain_text", "text": "開始"},
        "close": {"type": "plain_text", "text": "閉じる"},
        "blocks": [{"type": "input", "block_id": "course",
                    "label": {"type": "plain_text", "text": "今学期の履修科目"},
                    "element": {"type": "static_select", "action_id": "select", "options": options}}],
    }
