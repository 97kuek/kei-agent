"""時間記録のモジュールの、手で動かすコマンド（`kei-agent-module time <コマンド>`）。

- cards … Kei Agent がいる、時間を測るチャンネル（設定の prefixes。既定は 10_・20_・30_）に、固定する時間記録カードを
  投稿する。カードがもうあるチャンネルには置かない。投稿したら、Slack で各カードを手で固定する。
  Slack の鍵（SLACK_BOT_TOKEN）は ~/.config/kei-agent/secrets/kei-agent.zsh を source してから渡す
"""

from __future__ import annotations

import argparse
import asyncio
import os

from slack_sdk.web.async_client import AsyncWebClient

from kei_agent_a2a.api import Config, action_id, load_config, records, settings

from . import cards
from .entries import Entries, prefixes_of

NAME = "time"


async def post_cards(config: Config, client) -> int:
    """カードの無い、時間を測るチャンネルにカードを置く。置いた数を返す。"""
    entries = Entries(records(config, NAME))
    prefixes = tuple(prefixes_of(settings(config, NAME).get("prefixes")))

    def ids(name: str) -> str:
        return action_id(NAME, name)

    count, cursor = 0, None
    while True:
        response = await client.conversations_list(types="public_channel,private_channel", exclude_archived=True,
                                                   limit=200, cursor=cursor)
        for channel in response.get("channels", []):
            channel_id = str(channel.get("id") or "")
            if not channel.get("is_member") or not str(channel.get("name") or "").startswith(prefixes):
                continue
            if entries.card(channel_id):
                continue
            entry = entries.running_in(channel_id)
            posted = await client.chat_postMessage(channel=channel_id, text=cards.fallback_text(entry),
                                                   blocks=cards.blocks(ids, entry))
            entries.set_card(channel_id, str(posted["ts"]), ids(""))
            count += 1
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return count


def cards_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent-module time cards",
                                     description="時間を測るチャンネルに、固定する時間記録カードを投稿する")
    parser.parse_args(argv)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("SLACK_BOT_TOKEN がありません（~/.config/kei-agent/secrets/kei-agent.zsh を source してから動かしてください）")
        return 1
    count = asyncio.run(post_cards(load_config(), AsyncWebClient(token=token)))
    print(f"時間記録カードを {count} 件投稿しました。Slack で各カードを固定してください。")
    return 0


COMMANDS = {"cards": cards_main}
