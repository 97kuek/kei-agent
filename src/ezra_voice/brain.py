"""Codex CLI を相談相手として使う。会話は日ごとに1本、続きは resume でつなぐ。

`codex exec --json` は出来事を1行ずつ JSON で出す。返事が出た端から読み上げたいので、
その行を見ながら文を取り出して呼び戻す。形が変わっても困らないよう、最後の返事は
`-o`（ファイル）からも受け取り、そちらを本文として扱う。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# ふだんは速く、「じっくり考えて」と言った回だけ深く考える
FAST_ARGS = ("-c", "model_reasoning_effort=low")
DEEP_ARGS = ("-c", "model_reasoning_effort=high")
DEEP_WORDS = ("じっくり", "よく考え", "深く考え", "本気で考え")

_LIMIT = re.compile(r"usage limit", re.IGNORECASE)
_RESET_AT = re.compile(r"try again at ([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM)?)", re.IGNORECASE)
# 返事の文が入っている場所（Codex の出来事の形が変わっても拾えるよう、広めに見る）
_TEXT_KEYS = ("text", "message", "content", "delta")
_MESSAGE_TYPES = ("agent_message", "assistant_message", "message", "output_text")


@dataclass
class Turn:
    """1回のやりとりの結果。"""
    text: str = ""
    limit_reset: str | None = None
    failed: str | None = None
    session_id: str | None = None


def wants_deep(text: str) -> bool:
    return any(word in text for word in DEEP_WORDS)


def today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


class Codex:
    """Codex CLI を、同じ会話を続ける形で呼ぶ。"""

    def __init__(self, cwd: Path, state_dir: Path, instructions: str = "",
                 command: tuple[str, ...] = ("codex",)):
        self.cwd = cwd
        self.state_dir = state_dir
        self.instructions = instructions
        self.command = command

    # 会話（日ごとに1本）

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def last_message_path(self) -> Path:
        return self.state_dir / "last-message.txt"

    def session_id(self, day: str) -> str | None:
        try:
            saved = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return saved.get("session_id") if saved.get("day") == day else None

    def remember(self, day: str, session_id: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(json.dumps({"day": day, "session_id": session_id}), encoding="utf-8")

    def forget(self) -> None:
        """「新しく話そう」と言われたとき。"""
        self.session_path.unlink(missing_ok=True)

    # やりとり

    def build_command(self, session_id: str | None, deep: bool) -> list[str]:
        args = [*self.command, "exec"]
        if session_id:
            args += ["resume", session_id]
        args += ["--json", "--skip-git-repo-check", "-o", str(self.last_message_path)]
        return [*args, *(DEEP_ARGS if deep else FAST_ARGS), "-"]

    def ask(self, text: str, day: str, deep: bool | None = None,
            on_text: Callable[[str], None] | None = None) -> Turn:
        """1回話しかける。返事の文が届くたびに on_text を呼び、最後に結果を返す。"""
        deep = wants_deep(text) if deep is None else deep
        session_id = self.session_id(day)
        prompt = text if session_id else f"{self.instructions}\n\n---\n\n{text}".strip()
        turn = Turn(session_id=session_id)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.last_message_path.unlink(missing_ok=True)
        said: list[str] = []
        with subprocess.Popen(self.build_command(session_id, deep), cwd=self.cwd, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                event = _parse_event(line)
                if event is None:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    turn.session_id = str(event["thread_id"])
                message = _limit_message(event)
                if message:
                    turn.failed = message
                    turn.limit_reset = _reset_time(message)
                chunk = _message_text(event)
                if chunk and chunk not in said:
                    said.append(chunk)
                    if on_text:
                        on_text(chunk)
        turn.text = self._read_last_message() or "\n\n".join(said)
        if turn.session_id:
            self.remember(day, turn.session_id)
        return turn

    def _read_last_message(self) -> str:
        try:
            text = self.last_message_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        self.last_message_path.unlink(missing_ok=True)
        return text


def _parse_event(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _message_text(event: dict) -> str:
    for holder in (event.get("item"), event):
        if isinstance(holder, dict) and holder.get("type") in _MESSAGE_TYPES:
            for key in _TEXT_KEYS:
                value = holder.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, list):
                    joined = "".join(part.get("text", "") for part in value if isinstance(part, dict))
                    if joined.strip():
                        return joined
    return ""


def _limit_message(event: dict) -> str | None:
    error = event.get("error")
    candidates = [event.get("message"), error.get("message") if isinstance(error, dict) else None]
    for value in candidates:
        if isinstance(value, str) and _LIMIT.search(value):
            return value
    return None


def _reset_time(message: str) -> str:
    match = _RESET_AT.search(message)
    return match.group(1).strip() if match else ""
