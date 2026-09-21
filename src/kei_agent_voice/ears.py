"""耳。マイクから聞き続けて、確定した文字起こしを1つずつ渡す。

文字起こしそのものは macOS の `SpeechTranscriber`（`listen.swift`）。Python から呼べない API なので、
そこだけ Swift に置いて、標準出力を1行1件の JSON で受ける（docs/voice.md の9節）。

呼びかけの判定は `wake.py`。ここは「聞こえた文字を渡す」ところまで。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

LISTENER = Path(__file__).with_name("listen.swift")
# 先にコンパイルして使い回す。`swift <file>` は毎回コンパイルし、実測で聞き始めるまで
# 12 秒かかった（常駐サービスとしては長い）
CACHE = Path.home() / ".local/state/kei-agent/voice"
BUILT = "listen"


class EarsUnavailable(RuntimeError):
    pass


class Ears:
    """聞こえた文字を流す。`with` を抜けると、マイクを離す。"""

    def __init__(self, listener: Path | None = None, swift: str = "swift",
                 cache: Path | None = None):
        self.listener = listener or LISTENER
        self.swift = swift
        self.cache = cache or CACHE
        self.proc: subprocess.Popen[str] | None = None

    def build(self) -> Path:
        """コンパイル済みのものを返す。無い・古いときだけ作り直す。"""
        out = self.cache / BUILT
        source = self.listener.stat().st_mtime
        if out.exists() and out.stat().st_mtime >= source:
            return out
        self.cache.mkdir(parents=True, exist_ok=True)
        compiler = self.swift if self.swift != "swift" else "swiftc"
        log.info("聞く側をコンパイルします（初回だけ）: %s", out)
        self._compile(compiler, out)
        if not out.exists():
            raise EarsUnavailable(f"聞く側をコンパイルできません: {out}")
        return out

    def _compile(self, compiler: str, out: Path) -> None:
        done = subprocess.run([compiler, "-O", "-o", str(out), str(self.listener)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise EarsUnavailable(f"聞く側をコンパイルできません: {done.stderr.strip()[:300]}")

    def __enter__(self) -> Ears:
        if shutil.which(self.swift) is None:
            raise EarsUnavailable(
                f"{self.swift} が見つかりません（Xcode の Command Line Tools を入れてください）")
        if not self.listener.exists():
            raise EarsUnavailable(f"聞く側のプログラムがありません: {self.listener}")
        self.proc = subprocess.Popen(
            [str(self.build())], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    def heard(self) -> Iterator[str]:
        """確定した文字を、聞こえた順に渡す。聞き始められなければ EarsUnavailable。"""
        if self.proc is None or self.proc.stdout is None:
            raise EarsUnavailable("まだ聞き始めていません（with を使ってください）")
        for line in self.proc.stdout:
            event = _event(line)
            kind = event.get("kind")
            if kind == "ready":
                log.info("聞き始めました")
            elif kind == "final":
                yield str(event.get("text") or "")
            elif kind == "error":
                raise EarsUnavailable(str(event.get("text") or "聞けません"))


def _event(line: str) -> dict:
    line = (line or "").strip()
    if not line.startswith("{"):
        return {}
    try:
        found = json.loads(line)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}
