"""Slack の対象チャンネルに、固定する時間記録カードを一度だけ投稿する。"""

from __future__ import annotations

import asyncio
import os

from slack_sdk.web.async_client import AsyncWebClient

from kei_agent.config import load_config
from kei_agent.store import Store
from kei_agent.time_cards import blocks, fallback_text
from kei_agent.time_tracking import TimeTracker


def _target(name: str) -> bool:
    """研究・大学・仕事のテーマだけ。overview や改善チャンネルには置かない。"""
    return name.startswith(("10_", "20_", "30_"))


async def install() -> int:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN がありません")
    config = load_config()
    store = Store(config.db_path)
    tracker = TimeTracker(store)
    client = AsyncWebClient(token=token)
    cursor = None
    count = 0
    while True:
        response = await client.conversations_list(types="public_channel,private_channel", exclude_archived=True,
                                                   limit=200, cursor=cursor)
        for channel in response.get("channels", []):
            if not channel.get("is_member") or not _target(str(channel.get("name") or "")):
                continue
            channel_id = str(channel["id"])
            if store.time_card(channel_id) is not None:
                continue
            entry = tracker.active(config.allowed_user_id)
            entry = entry if entry and entry.channel_id == channel_id else None
            posted = await client.chat_postMessage(channel=channel_id, text=fallback_text(entry), blocks=blocks(entry))
            store.upsert_time_card(channel_id, str(posted["ts"]))
            count += 1
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return count


def main() -> None:
    count = asyncio.run(install())
    print(f"時間記録カードを {count} 件投稿しました。Slack で各カードを固定してください。")


if __name__ == "__main__":  # pragma: no cover
    main()
