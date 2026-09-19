"""VOICEVOX で読み上げる。文ができた端から鳴らし、Enter で止められるようにする。

VOICEVOX アプリ（または engine）を立ち上げておくと、`127.0.0.1:50021` で待ち受ける。
立ち上がっていないときは、黙って文字だけの会話に落とす（声は出ないが、対話は続けられる）。
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 50021
# 少し速めのほうが、待ち時間の不満が減る（聞き比べて 1.2 倍に決めた）
DEFAULT_SPEED = 1.2
# 四国めたん／ノーマル。はきはきして、暗くならない声（2026-09-19 に聞き比べて決めた）
DEFAULT_SPEAKER = 2
TIMEOUT_SECONDS = 30


class Voicevox:
    """VOICEVOX の待ち受け口。文を1つずつ音声にする。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 speaker: int = DEFAULT_SPEAKER, speed: float = DEFAULT_SPEED):
        self.base = f"http://{host}:{port}"
        self.speaker = speaker
        self.speed = speed

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/version", timeout=2):
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def speakers(self) -> list[tuple[int, str]]:
        """使える声の一覧（ID と名前）。声を選ぶときに使う。"""
        with urllib.request.urlopen(f"{self.base}/speakers", timeout=TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
        return [(style["id"], f"{s['name']}／{style['name']}") for s in data for style in s.get("styles", [])]

    def synthesize(self, sentence: str) -> bytes:
        query = self._post(f"/audio_query?{urllib.parse.urlencode({'text': sentence, 'speaker': self.speaker})}")
        query["speedScale"] = self.speed
        return self._post(f"/synthesis?{urllib.parse.urlencode({'speaker': self.speaker})}", query, raw=True)

    def _post(self, path: str, body: dict | None = None, raw: bool = False):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        req = urllib.request.Request(f"{self.base}{path}", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read() if raw else json.load(resp)


class Speaker:
    """読み上げの列。裏で1文ずつ鳴らし、`stop()` で今の1文ごと捨てる。"""

    def __init__(self, engine: Voicevox | None = None, play_command: tuple[str, ...] = ("afplay",)):
        self.engine = engine
        self.play_command = play_command
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.enabled = bool(engine and engine.available())
        self._player: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def say(self, sentence: str) -> None:
        if sentence.strip():
            self.queue.put(sentence.strip())

    def stop(self) -> None:
        """読み上げをやめる（長いと思ったときに使う）。"""
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break
        with self._lock:
            if self._player and self._player.poll() is None:
                self._player.terminate()

    def wait(self) -> None:
        self.queue.join()

    def close(self) -> None:
        self.stop()
        self.queue.put(None)

    def _run(self) -> None:
        while True:
            sentence = self.queue.get()
            if sentence is None:
                self.queue.task_done()
                return
            try:
                if self.enabled:
                    self._play(sentence)
            except Exception:
                log.warning("読み上げに失敗しました（声なしで続けます）", exc_info=True)
                self.enabled = False
            finally:
                self.queue.task_done()

    def _play(self, sentence: str) -> None:
        assert self.engine is not None
        wav = self.engine.synthesize(sentence)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = Path(f.name)
        try:
            with self._lock:
                self._player = subprocess.Popen([*self.play_command, str(path)],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._player.wait()
        finally:
            with self._lock:
                self._player = None
            path.unlink(missing_ok=True)
