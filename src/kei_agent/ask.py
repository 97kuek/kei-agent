"""Slack の外（机の上の音声対話など）から Kei Agent に依頼を渡す口（docs/architecture.md）。

Slack のトークンを増やさないため、同じ Mac の中にファイルを置いて渡す。
Kei Agent は数秒ごとにここを見て、チャンネルにスレッドを立ててから、いつもどおり作業する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from kei_agent.config import Config, load_config

ASK_DIR = "asks"
# Kei Agent が置かれた依頼を拾うまでの間隔（秒）
POLL_SECONDS = 3.0
# 依頼（Claude を動かす）と、記録だけ（決まったことを残す）
KINDS = ("request", "note")


@dataclass(frozen=True)
class PendingAsk:
    path: Path
    payload: dict


def ask_dir(config: Config) -> Path:
    return config.state_dir / ASK_DIR


def _write_payload(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_ask(config: Config, theme: str, text: str, kind: str = "request") -> Path:
    """依頼（または記録）を1つ置く。Kei Agent が拾うとファイルは消える。"""
    if kind not in KINDS:
        raise ValueError(f"kind は {' / '.join(KINDS)} のどれかにしてください: {kind}")
    directory = ask_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.time():.3f}-{uuid.uuid4().hex[:8]}.json"
    _write_payload(path, {"theme": theme, "text": text, "kind": kind, "created_at": time.time()})
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


def recover_asks(config: Config) -> None:
    """前回のプロセスが残した claim を、起動時に限って回収する。"""
    directory = ask_dir(config)
    if not directory.is_dir():
        return
    for path in directory.glob("*.json.processing"):
        try:
            os.replace(path, path.with_name(path.name.removesuffix(".processing")))
        except FileNotFoundError:
            continue


def claim_asks(config: Config) -> list[PendingAsk]:
    """未処理の依頼を原子的に claim する。"""
    directory = ask_dir(config)
    if not directory.is_dir():
        return []

    asks = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
            continue
        if not isinstance(payload, dict):
            path.unlink(missing_ok=True)
            continue
        processing_path = path.with_name(f"{path.name}.processing")
        try:
            os.replace(path, processing_path)
        except FileNotFoundError:
            continue
        asks.append(PendingAsk(processing_path, payload))
    return asks


def complete_ask(item: PendingAsk) -> None:
    item.path.unlink(missing_ok=True)


def retry_ask(item: PendingAsk) -> None:
    os.replace(item.path, item.path.with_name(item.path.name.removesuffix(".processing")))


def record_thread(item: PendingAsk, thread_ts: str) -> None:
    payload = {**item.payload, "thread_ts": thread_ts}
    _write_payload(item.path, payload)
    item.payload["thread_ts"] = thread_ts


def main() -> None:
    """`kei-agent-ask --theme amr-query "〜して"`。声のレイヤからも、手でも使える。"""
    parser = argparse.ArgumentParser(description="Slack の外から Kei Agent に依頼を渡す")
    parser.add_argument("text", help="依頼の文（そのままスレッドに載る）")
    parser.add_argument("--theme", required=True, help="テーマ（Slack のチャンネル名）")
    parser.add_argument("--note", action="store_true", help="作業させず、決まったこととして記録だけする")
    args = parser.parse_args()
    if not args.text.strip():
        sys.exit("依頼の文が空です")
    path = write_ask(load_config(), args.theme, args.text.strip(), "note" if args.note else "request")
    print(f"渡しました: {path}")
