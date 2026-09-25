"""声の会話の全文を、日ごとのファイルに残す（docs/architecture.md の「声のレイヤ」）。

読み返さない前提の控え。依頼は Slack のスレッドに残るので、ここは「何を話したか」だけ。
毎晩の保守で 30 日より古いものを消す（`maintenance.py` が `overview/voice/*.md` を見ている）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

VOICE_DIR = "voice"
HEAD = "# 声の会話 {day}\n\n依頼は Slack に残る。ここは全文の控え。\n"


def path_for(overview_dir: Path, day: str) -> Path:
    return overview_dir / VOICE_DIR / f"{day}.md"


def append(overview_dir: Path, who: str, text: str, now: float | None = None) -> None:
    """1つの発言を書き足す。書けなくても会話は続ける（記録のために黙るのは本末転倒）。"""
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    path = path_for(overview_dir, day)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(HEAD.format(day=day), encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {who}（{time.strftime('%H:%M', time.localtime(now))}）\n\n{text.strip()}\n")
    except OSError as e:
        log.warning("声の会話を残せませんでした: %s", e)
