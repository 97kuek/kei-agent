"""デプロイのあと、全部のプロセスが新しい版で動いているかを確かめる（deploy/update.sh から呼ぶ）。

担当と本体の口（声からの問い合わせ）は名刺の version、Notion ゲートウェイは /health の version を見る。
起動には少しかかるので、WAIT_SECONDS まで見直しながら待つ。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import aiohttp

from kei_agent.config import Config, gateway_endpoint, load_config

WAIT_SECONDS = 90
POLL_SECONDS = 3
CARD_PATH = "/.well-known/agent-card.json"


def targets(config: Config) -> dict[str, str]:
    """確かめるプロセスと、版を読む URL。"""
    found = {name: url.rstrip("/") + CARD_PATH for name, url in config.a2a.agents.items()}
    if config.a2a.orchestrator:
        found["本体"] = config.a2a.orchestrator.rstrip("/") + CARD_PATH
    found["notion-gateway"] = gateway_endpoint(config.notion_gateway_url, "health")
    return found


async def running_version(http: aiohttp.ClientSession, url: str) -> str:
    """そのプロセスが起動したときの commit。答えなければ空。"""
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return str((await resp.json(content_type=None)).get("version") or "")
    except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError):
        return ""


async def stale(config: Config, expected: str, wait: float = WAIT_SECONDS) -> dict[str, str]:
    """wait 秒たっても新しい版にならないもの（名前 → いまの版。答えなければ空）。"""
    left = targets(config)
    found: dict[str, str] = {}
    deadline = time.monotonic() + wait
    async with aiohttp.ClientSession() as http:
        while True:
            for name, url in list(left.items()):
                found[name] = await running_version(http, url)
                if found[name] == expected:
                    del left[name]
            if not left or time.monotonic() >= deadline:
                return {name: found.get(name, "") for name in left}
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m kei_agent.deploy_check")
    parser.add_argument("commit", help="動いているはずの版（git rev-parse --short=12 HEAD）")
    args = parser.parse_args()
    config = load_config()
    names = list(targets(config))
    left = asyncio.run(stale(config, args.commit))
    if not left:
        print(f"全部（{len(names)}）が新しい版 {args.commit} で動いています: {'、'.join(names)}")
        return
    for name, found in left.items():
        print(f"まだ新しい版で動いていません: {name}（{found or '答えなし'}）", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
