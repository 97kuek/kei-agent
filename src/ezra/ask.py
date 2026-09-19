"""Slack の外（机の上の音声対話など）から Ezra に依頼を渡す口（docs/plan.md の13章）。

Slack のトークンを増やさないため、同じ Mac の中にファイルを置いて渡す。
Ezra は数秒ごとにここを見て、チャンネルにスレッドを立ててから、いつもどおり作業する。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from ezra.config import Config, load_config

ASK_DIR = "asks"
# Ezra が置かれた依頼を拾うまでの間隔（秒）
POLL_SECONDS = 3.0
# 依頼（Claude を動かす）と、記録だけ（決まったことを残す）
KINDS = ("request", "note")


def ask_dir(config: Config) -> Path:
    return config.state_dir / ASK_DIR


def write_ask(config: Config, theme: str, text: str, kind: str = "request") -> Path:
    """依頼（または記録）を1つ置く。Ezra が拾うとファイルは消える。"""
    if kind not in KINDS:
        raise ValueError(f"kind は {' / '.join(KINDS)} のどれかにしてください: {kind}")
    directory = ask_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.time():.3f}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(
        {"theme": theme, "text": text, "kind": kind, "created_at": time.time()}, ensure_ascii=False,
    ), encoding="utf-8")
    return path


def pending_asks(config: Config) -> list[tuple[Path, dict]]:
    """置かれている依頼を、古い順に。読めないファイルは捨てる。"""
    directory = ask_dir(config)
    if not directory.is_dir():
        return []
    asks = []
    for path in sorted(directory.glob("*.json")):
        try:
            asks.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
    return asks


def main() -> None:
    """`ezra-ask --theme amr-query "〜して"`。声のレイヤからも、手でも使える。"""
    parser = argparse.ArgumentParser(description="Slack の外から Ezra に依頼を渡す")
    parser.add_argument("text", help="依頼の文（そのままスレッドに載る）")
    parser.add_argument("--theme", required=True, help="テーマ（Slack のチャンネル名）")
    parser.add_argument("--note", action="store_true", help="作業させず、決まったこととして記録だけする")
    args = parser.parse_args()
    if not args.text.strip():
        sys.exit("依頼の文が空です")
    path = write_ask(load_config(), args.theme, args.text.strip(), "note" if args.note else "request")
    print(f"渡しました: {path}")
