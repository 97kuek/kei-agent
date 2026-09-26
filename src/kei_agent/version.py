"""動いている版（起動したときのリポジトリの commit）。

本体は起動したときに担当（A2A の各プロセス）の名刺の版と見比べて、古い版のまま動いている担当を起動し直す。
取り込んだのに起動し直していないときは、スケジューラが知らせる。
"""

from __future__ import annotations

import subprocess

from kei_agent.config import REPO_ROOT

UNKNOWN = "unknown"


def on_disk() -> str:
    """いまリポジトリにある版。取り込んだあと起動し直していなければ、RUNNING と違う。"""
    try:
        proc = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else UNKNOWN


# このプロセスが起動したときの版（import した時点で決める）
RUNNING = on_disk()


def differs(theirs: str, mine: str | None = None) -> bool:
    """版が違うか。どちらかが分からなければ、違うとは言わない。"""
    mine = RUNNING if mine is None else mine
    return UNKNOWN not in (theirs, mine) and bool(theirs) and theirs != mine
