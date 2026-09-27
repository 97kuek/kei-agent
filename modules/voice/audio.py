"""マイクから生の音を取り、返ってきた生の音を鳴らす（docs/architecture.md の「声のレイヤ」）。

OpenAI Realtime API は音をそのままやりとりするので、文字起こしも音声合成も要らない。
ここが要るのは**音の出し入れだけ**。

`ffmpeg` を使う（この Mac に入っている）。追加の依存を増やさないため。

| | やり方 | 測ったこと |
|---|---|---|
| 録る | `ffmpeg -f avfoundation -i :default -ar 24000 -ac 1 -f s16le -` | 1秒で 42,880 バイト取れた |
| 鳴らす | `ffmpeg ... -f audiotoolbox -` の標準入力に PCM を流し込む | 止めるときはプロセスを殺す |

**割り込み（barge-in）はプロセスを殺して実現する。** 鳴っている途中で話しかけられたら、
鳴らしている `ffmpeg` を殺して、溜まっている音を捨てる。次に書くときに起こし直す（数十ミリ秒）。
バッファに書いた音を捨てるほかの手が無いので、これがいちばん確実。

**どこまで聞かれたかを数えておく。** 割り込まれたとき、モデルには「ここまでしか聞かれていない」と
伝える必要がある（伝えないと、モデルは全部聞かれたつもりで話を続ける）。
書いたバイト数と鳴らし始めた時刻から、実際に鳴った長さを見積もる。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterator

log = logging.getLogger(__name__)

# avfoundation の入力。`:default` はいまの既定のマイク（`ffmpeg -f avfoundation -list_devices true -i ""` で一覧）
MIC_ENV = "KEI_AGENT_MIC"
DEFAULT_MIC = ":default"
# Realtime API がやりとりする形（PCM16 / 24kHz / モノラル）
RATE = 24000
WIDTH = 2
# 1回に送る長さ。短いほど反応が早いが、送る回数が増える
CHUNK_MS = 40
CHUNK_BYTES = RATE * WIDTH * CHUNK_MS // 1000

_FFMPEG = ("ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation")
# 鳴らす側。`ffplay` は `-ac` を受け付けない（`Option not found` で黙って死ぬ）ので使わない
_PLAY = ("ffmpeg", "-hide_banner", "-loglevel", "error")


class Unavailable(Exception):
    """声で話せない（`ffmpeg` が無い、マイクが使えない、鍵が無い・断られた）。`live` もこれを使う。"""


def ms_of(pcm: bytes) -> int:
    """PCM の長さをミリ秒で。"""
    return len(pcm) * 1000 // (RATE * WIDTH)


class Microphone:
    """マイクの音を、決まった長さずつ返す。`with` で開けて、抜けると閉じる。"""

    def __init__(self, device: str = "", env: dict | None = None):
        env = os.environ if env is None else env
        self.device = device or env.get(MIC_ENV) or DEFAULT_MIC
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> Microphone:
        command = [*_FFMPEG, "-i", self.device, "-ar", str(RATE), "-ac", "1", "-f", "s16le", "-"]
        try:
            self.proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            raise Unavailable("ffmpeg が入っていません（brew install ffmpeg）") from e
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.kill()
        proc.wait(timeout=5)

    def chunks(self) -> Iterator[bytes]:
        """音を `CHUNK_MS` ずつ返す。マイクを閉じるまで止まらない。"""
        if self.proc is None or self.proc.stdout is None:
            raise Unavailable("マイクを開けていません")
        while True:
            chunk = self.proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                # ffmpeg が落ちた。理由を残す（許可が無いときはここに出る）
                raise Unavailable(self._why())
            yield chunk

    def _why(self) -> str:
        if self.proc is None or self.proc.stderr is None:
            return "マイクが閉じました"
        reason = self.proc.stderr.read().decode("utf-8", "replace").strip()
        return reason.splitlines()[-1] if reason else "マイクが閉じました"


class Speaker:
    """返ってきた音を鳴らす。**途中で止められる**（割り込みのため）。"""

    def __init__(self, command: tuple[str, ...] = _PLAY):
        self.command = command
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._started = 0.0
        self._written_ms = 0

    def write(self, pcm: bytes) -> None:
        """音を足す。鳴っていなければ、ここで鳴らし始める。"""
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            if self.proc is None or self.proc.stdin is None:
                return
            try:
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                # 殺した直後に書くと起きる。次の write で起こし直す
                self.proc = None
                return
            self._written_ms += ms_of(pcm)

    def begin_item(self) -> None:
        """次の返事を鳴らし始める。**長さは返事ごとに数え直す**（割り込みで伝えるのは返事の中の位置）。

        前の返事がまだ鳴っていれば、次の返事はそれが鳴り終わってから鳴り始める。
        """
        with self._lock:
            now = time.monotonic()
            ends = self._started + self._written_ms / 1000 if self._started else now
            self._started, self._written_ms = max(now, ends), 0

    @property
    def played_ms(self) -> int:
        """**実際に鳴った長さ**の見積り。書いた長さと、経った時間の小さいほう。

        割り込まれたとき、モデルに「ここまでしか聞かれていない」と伝えるのに使う。
        """
        if not self._started:
            return 0
        elapsed = int((time.monotonic() - self._started) * 1000)
        return min(self._written_ms, max(elapsed, 0))

    @property
    def speaking(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.played_ms < self._written_ms

    async def wait_until_done(self) -> None:
        """書き込んだPCMを鳴らし終えるまで待つ。"""
        remaining_ms = max(self._written_ms - self.played_ms, 0)
        if remaining_ms:
            await asyncio.sleep(remaining_ms / 1000)

    def stop(self) -> int:
        """いま鳴っている音を捨てる。**鳴った長さ**を返す（モデルに伝えるため）。

        溜まった音を捨てる手がほかに無いので、プロセスごと殺す。
        """
        with self._lock:
            played = self.played_ms
            self._kill()
            self._started, self._written_ms = 0.0, 0
            return played

    def close(self) -> None:
        with self._lock:
            self._kill()

    def _start(self) -> None:
        command = [*self.command, "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
                   "-f", "audiotoolbox", "-"]
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise Unavailable("ffmpeg が入っていません（brew install ffmpeg）") from e
        self._started, self._written_ms = time.monotonic(), 0

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        proc.kill()
        proc.wait(timeout=5)
