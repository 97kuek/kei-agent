#!/usr/bin/env python3
"""Kei Agent のジョブを投入・確認・取り消すための小さなCLI（標準ライブラリだけで動く）。

このスクリプトは依頼をファイルに書くだけで、実際に pueue へ投入するのは Kei Agent 本体。
Claude Code の sandbox の中から呼ばれるので、カレントディレクトリ（テーマの作業用ディレクトリ）の外には書かない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

REQUESTS_DIR = Path(".kei-agent/requests")
JOBS_DIR = Path(".kei-agent/jobs")


def _write_request(payload: dict) -> str:
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "channel": os.environ.get("KEI_AGENT_CHANNEL", ""),
        "thread_ts": os.environ.get("KEI_AGENT_THREAD_TS", ""),
        "created_at": time.time(),
        **payload,
    }
    tmp = REQUESTS_DIR / f".{request_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(REQUESTS_DIR / f"{request_id}.json")
    return request_id


def cmd_submit(args: argparse.Namespace) -> int:
    script = Path(args.script)
    if script.is_absolute() or ".." in script.parts:
        print("script はカレントディレクトリからの相対パスで指定してください", file=sys.stderr)
        return 2
    if not script.is_file():
        print(f"script が見つかりません: {script}", file=sys.stderr)
        return 2
    if not os.environ.get("KEI_AGENT_THREAD_TS"):
        print("KEI_AGENT_THREAD_TS がありません。Kei Agent から起動された claude でだけ使えます", file=sys.stderr)
        return 2
    request_id = _write_request({
        "action": "submit",
        "name": args.name,
        "script": str(script),
        "args": args.script_args,
    })
    print(f"ジョブの投入を依頼しました（request_id={request_id}）。")
    print("Kei Agent がこの回の作業のあとに pueue へ投入し、終わったらこのスレッドの会話を再開します。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    states = []
    if JOBS_DIR.is_dir():
        for p in sorted(JOBS_DIR.glob("*.json")):
            try:
                states.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    if args.job_id is not None:
        states = [s for s in states if s.get("job_id") == args.job_id]
    pending = sorted(p.stem for p in REQUESTS_DIR.glob("*.json")) if REQUESTS_DIR.is_dir() else []
    print(json.dumps({"jobs": states, "pending_requests": pending}, ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    request_id = _write_request({"action": "cancel", "job_id": args.job_id})
    print(f"ジョブ {args.job_id} の取り消しを依頼しました（request_id={request_id}）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei_agent_job")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit", help="ジョブを投入する")
    p.add_argument("--name", required=True, help="ジョブの短い名前")
    p.add_argument("script", help="実行するスクリプト（.py / .sh）。カレントディレクトリからの相対パス")
    p.add_argument("script_args", nargs=argparse.REMAINDER, help="スクリプトに渡す引数（-- のあとに書く）")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="ジョブの状態を表示する")
    p.add_argument("job_id", nargs="?", type=int)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel", help="ジョブを取り消す")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_cancel)

    args = parser.parse_args(argv)
    if getattr(args, "script_args", None) and args.script_args[0] == "--":
        args.script_args = args.script_args[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
