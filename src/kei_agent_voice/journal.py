"""声の会話の全文を、日ごとのファイルに残す（毎晩の保守で30日で消える）。"""

from __future__ import annotations

import time
from pathlib import Path

from kei_agent.config import Config
from kei_agent.themes import OVERVIEW_DIR

VOICE_DIR = "voice"


def path_for(config: Config, day: str) -> Path:
    return config.research_root / OVERVIEW_DIR / VOICE_DIR / f"{day}.md"


def append(config: Config, day: str, who: str, text: str, now: float | None = None) -> Path:
    """1つの発言を書き足す。決まったことは Slack に残るので、ここは読み返さない前提の記録。"""
    path = path_for(config, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# 声の会話 {day}\n\n決まったことと依頼は Slack に残る。ここは全文の控え。\n", encoding="utf-8")
    stamp = time.strftime("%H:%M", time.localtime(now))
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {who}（{stamp}）\n\n{text.strip()}\n")
    return path
