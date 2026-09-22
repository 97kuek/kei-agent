"""聞いて、答える。声のレイヤの本筋（docs/voice.md）。

**考えるのは全部 Realtime API**（`live.py`）。ここがするのは、繋ぎ目の世話だけ。

- マイクの開け閉め（Slack の App Home から押される。**既定は閉じたまま**）
- 本体から来た知らせを、同じ声で喋らせる
- 話した中身を控えに残す（`journal.py`）
- 机の上のロボットの顔を変える（`face.py`。まだ買っていないので、ふだんは何もしない）

前は自分で文字起こしをして、言葉で道を振り分けて、音声合成していた。**全部やめた**。
言葉で振り分けていたせいで、「明日の予定」にも「今週の予定」にも**今日の予定を答えていた**
（docs/voice.md の3節）。いまは依頼者のことを**道具として渡す**（`tools.py`）ので、
いつのことかはモデルが引数で渡してくる。
"""

from __future__ import annotations

import asyncio
import logging

from kei_agent.config import Config, load_config
from kei_agent_voice import journal, live
from kei_agent_voice.face import Face
from kei_agent_voice.tools import Tools

log = logging.getLogger(__name__)


class VoiceSession:
    """マイクを開けているあいだ、Realtime API と繋がっている。"""

    def __init__(self, held: dict, config: Config | None = None,
                 brain: live.Live | None = None, face: Face | None = None):
        self.held = held
        self.config = config or load_config()
        self.face = face or Face()
        self.brain = brain or live.Live(Tools(held, self.config))
        self._talking: asyncio.Task | None = None
        self._notices: asyncio.Queue[str] = asyncio.Queue()
        self._notice_worker: asyncio.Task | None = None

    @property
    def listening(self) -> bool:
        return self._talking is not None and not self._talking.done()

    def set_listening(self, on: bool) -> None:
        """マイクを開ける・閉じる。

        閉じるときは**繋がりごと切る**（無視するだけだと、録って送り続けることになる）。
        """
        if on and not self.listening:
            self._talking = asyncio.create_task(self._talk())
            log.info("マイクを開けました")
        elif not on and self.listening:
            if self._talking is not None:
                self._talking.cancel()
            self._talking = None
            self.brain.close()
            log.info("マイクを閉じました")

    async def run(self, listening: bool = False) -> None:
        """立ち上げ。**既定ではマイクを開けない**（docs/voice.md の7節）。"""
        self._notice_worker = asyncio.create_task(self._speak_notices())
        if listening:
            self.set_listening(True)
        try:
            # 開け閉めは本体（Slack）から押されるので、ここは待つだけ
            await asyncio.Event().wait()
        finally:
            talking = self._talking
            self.set_listening(False)
            self._notice_worker.cancel()
            tasks = [self._notice_worker]
            if talking is not None:
                tasks.append(talking)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _speak_notices(self) -> None:
        while True:
            text = await self._notices.get()
            try:
                await self.brain.say_once(text, on_said=self._write)
            except live.Unavailable as e:
                log.warning("知らせを喋れません: %s", e)
            except Exception:
                log.exception("知らせの読み上げに失敗しました")
            finally:
                self._notices.task_done()

    async def _talk(self) -> None:
        try:
            await self.brain.run(on_said=self._write)
        except asyncio.CancelledError:
            raise
        except live.Unavailable as e:
            # 鍵が無い日でも落とさない。顔と、本体への返事だけは動く
            log.warning("声で話せません: %s", e)
        except Exception:
            log.exception("声のやりとりが止まりました")

    def announce(self, text: str, expression: str = "") -> None:
        """顔をすぐ変え、通知を既存接続かマイクなしの通知workerに渡す。"""
        if expression:
            self.face.show(expression)
        if not text.strip():
            return
        if self.listening:
            self.brain.announce(text)
        else:
            self._notices.put_nowait(text)

    def _write(self, who: str, text: str) -> None:
        journal.append(self.config.overview_dir, who, text)
