"""聞いて、答える。声のレイヤの本筋。

呼ばれたら、まず相槌をすぐ返す（Codex の返事を待つ数秒、無言だと壊れて見える）。
そのあと、手元で答えられるものは即答し（速い道）、それ以外は考える（遅い道）。

いまは遅い道の相手（Codex）をまだ繋いでいない。繋ぐまでは、その旨を言う。

聞く側（`ears.py`）は止まらない生成器なので、別のスレッドで回して、聞こえた文字を
待ち行列に入れる。A2A サーバー（uvicorn）と同じプロセスで、邪魔をしないように。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime

from kei_agent_voice import fast, wake
from kei_agent_voice.ears import Ears, EarsUnavailable
from kei_agent_voice.mouth import Mouth

log = logging.getLogger(__name__)

# 呼ばれたことへの返事。考えている間、無言にしない
NODDED = "うん。"
# 遅い道を繋ぐまでの返事
NOT_YET = "ごめん、まだ考える相手につながってないんだ。Slack に頼んでみて。"


class VoiceSession:
    """マイクから聞いて、答える。`executor.held` に、本体が押してきたものが入っている。"""

    def __init__(self, held: dict, mouth: Mouth | None = None, ears: Ears | None = None):
        self.held = held
        self.mouth = mouth or Mouth()
        self.ears = ears or Ears()
        self.heard: asyncio.Queue[str] = asyncio.Queue()
        self._stop = threading.Event()

    async def run(self) -> None:
        """聞こえたものを、順に捌く。止めるには、このタスクを cancel する。"""
        loop = asyncio.get_running_loop()
        reader = threading.Thread(target=self._listen, args=(loop,), daemon=True)
        reader.start()
        try:
            while True:
                text = await self.heard.get()
                try:
                    await self.handle(text)
                except Exception:
                    log.exception("聞こえたことを捌けませんでした: %r", text[:80])
        finally:
            self._stop.set()

    def _listen(self, loop: asyncio.AbstractEventLoop) -> None:
        """別スレッドで聞き続け、聞こえた文字を待ち行列に渡す。"""
        try:
            with self.ears as ears:
                for text in ears.heard():
                    if self._stop.is_set():
                        return
                    loop.call_soon_threadsafe(self.heard.put_nowait, text)
        except EarsUnavailable as e:
            # 耳が無くても、口（本体からの知らせ）は動く。落とさない
            log.warning("聞けないので、声のレイヤは口だけで動きます: %s", e)
        except Exception:
            log.exception("聞く側が止まりました")

    async def handle(self, text: str, now: datetime | None = None) -> None:
        """聞こえた1件を捌く。呼ばれていなければ、何もしない。"""
        if not wake.called(text):
            return
        asked = wake.request(text)
        log.info("呼ばれました: %r", asked[:80])
        if not asked:
            # 名前だけ呼ばれた。相槌だけ返す
            await self.say(NODDED)
            return
        answer = fast.answer(asked, self.held, now or datetime.now())
        if answer is not None:
            await self.say(answer)
            return
        # 遅い道。考える間、黙らない
        await self.say(NODDED)
        await self.say(NOT_YET)

    async def say(self, text: str) -> None:
        """喋る。合成と再生で止まるので、別のスレッドに出す。"""
        await asyncio.get_running_loop().run_in_executor(None, self.mouth.say, text)
