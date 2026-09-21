"""喋る口。Stack-chan がいればそちらで、いなければ Mac のスピーカーで鳴らす。

合成は必ず Mac 側でやる（本体の TTS は中国語・英語向けで、日本語の声を選べない。docs/voice.md）。
Stack-chan には、できあがった wav を投げ込むだけ。リップシンクは本体が再生音から自動で作る。

`stackchan-atama` の口:
- `GET  /status`                   … 生きているか
- `POST /play_wav`                 … wav を投げる（再生中は自動でリップシンク）
- `GET  /face?expression=<6種>`    … 表情
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from kei_agent_voice import reading
from kei_agent_voice.speech import Voicevox

log = logging.getLogger(__name__)

# Stack-chan の住所（秘密情報のファイル）。書かなければ Mac のスピーカーで鳴らす
URL_ENV = "KEI_AGENT_STACKCHAN_URL"
# 生きているかを見に行くときの待ち時間。机の上の LAN なので短くてよい
PROBE_SECONDS = 1.5
SEND_SECONDS = 30


class Mouth:
    """声を出すところ。出し先は、呼ばれるたびに決める（電源を切っていることがあるので）。"""

    def __init__(self, engine: Voicevox | None = None, url: str = "",
                 play_command: tuple[str, ...] = ("afplay",)):
        self.engine = engine or Voicevox.from_env()
        self.url = (url or os.environ.get(URL_ENV, "")).rstrip("/")
        self.play_command = play_command

    def stackchan_ready(self) -> bool:
        """Stack-chan が待ち受けているか。住所を書いていなければ、いつも False。"""
        if not self.url:
            return False
        try:
            with urllib.request.urlopen(f"{self.url}/status", timeout=PROBE_SECONDS):
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def say(self, text: str) -> None:
        """喋る。長い返事は文ごとに分けて、**出だしから先に鳴らす**。

        合成できなければ、何もしない（黙るほうが、途中で止まるよりまし）。
        """
        for sentence in reading.sentences(text):
            if not self._say_one(sentence):
                return

    def _say_one(self, sentence: str) -> bool:
        try:
            wav = self.engine.synthesize(sentence)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("音声にできません（%s は動いていますか）: %s", self.engine.base, e)
            return False
        if self.stackchan_ready():
            if self._send(wav):
                return True
            log.info("Stack-chan に送れなかったので、Mac のスピーカーで鳴らします")
        self._play(wav)
        return True

    def face(self, expression: str) -> None:
        """表情を変える。Stack-chan がいなければ何もしない。"""
        if not self.stackchan_ready():
            return
        try:
            with urllib.request.urlopen(f"{self.url}/face?expression={expression}", timeout=PROBE_SECONDS):
                return
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.info("表情を変えられません: %s", e)

    def _send(self, wav: bytes) -> bool:
        req = urllib.request.Request(f"{self.url}/play_wav", data=wav, method="POST",
                                     headers={"Content-Type": "audio/wav"})
        try:
            with urllib.request.urlopen(req, timeout=SEND_SECONDS):
                return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("Stack-chan に送れません: %s", e)
            return False

    def _play(self, wav: bytes) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "say.wav"
            path.write_bytes(wav)
            subprocess.run([*self.play_command, str(path)], check=False)
