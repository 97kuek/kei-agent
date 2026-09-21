"""聞いて、答える。声のレイヤの本筋。

道は2本ある（docs/voice.md の3節）。

| | 速い道 | 遅い道 |
|---|---|---|
| 何に答えるか | 時刻・予定・締切・Kei Agent の様子 | それ以外の相談 |
| 誰が答えるか | 手元に押されてきたデータ（0秒） | Codex（約5秒） |

遅い道は5秒かかり、**返事は途中では出てこない**（`think.py` の実測）。だから先に相槌を返す。
無言の5秒は「壊れている」に見える。

作業の依頼は、**読み上げて確認してから**渡す（docs/voice.md の8節）。文字起こしは必ず誤るので、
動き出してからでは戻せない。確認の返事には呼びかけを要らないことにした（「けい、うん」とは言わない）。

聞く側（`ears.py`）は止まらない生成器なので、別のスレッドで回して、聞こえた文字を
待ち行列に入れる。A2A サーバー（uvicorn）と同じプロセスで、邪魔をしないように。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime

from kei_agent import ask as asks
from kei_agent.config import Config, load_config
from kei_agent_voice import fast, journal, think, wake
from kei_agent_voice.ears import Ears, EarsUnavailable
from kei_agent_voice.mouth import Mouth

log = logging.getLogger(__name__)

# 呼ばれたことへの返事。考えている間、無言にしない
NODDED = "うん。"
# 深く考えるように頼まれた回。5秒では済まないので、そう言う
THINKING = "ちょっと考えるね。"
HANDED = "渡したよ。終わったら知らせるね。"
DROPPED = "じゃあ、やめとくね。"
UNSURE = "よく分からなかったから、渡さずにおくね。"
FRESH = "うん、新しく話そう。"

# 確認の返事
YES = ("はい", "うん", "いいよ", "いい", "おねがい", "お願い", "そう", "オーケー", "おーけー", "ok", "yes")
NO = ("いや", "やめ", "ちがう", "違う", "だめ", "ダメ", "いらない", "キャンセル", "no")
# 会話を切り直す合図
FRESH_WORDS = ("新しく話", "忘れて", "別の話", "話を変え")
# 確認を待つ時間。これを過ぎたら、あとの「うん」で勝手に動き出さないように捨てる
CONFIRM_SECONDS = 60.0


class VoiceSession:
    """マイクから聞いて、答える。`executor.held` に、本体が押してきたものが入っている。"""

    def __init__(self, held: dict, mouth: Mouth | None = None, ears: Ears | None = None,
                 codex: think.Codex | None = None, config: Config | None = None):
        self.held = held
        self.mouth = mouth or Mouth()
        self.config = config or load_config()
        self.codex = codex or think.Codex(self.config.research_root, self.config.state_dir / "voice")
        self._make_ears = (lambda: ears) if ears is not None else Ears
        self.heard: asyncio.Queue[str] = asyncio.Queue()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.ears: Ears | None = None
        # 読み上げて確認している依頼（返事をもらうまで渡さない）
        self._pending: think.Ask | None = None
        self._pending_at = 0.0

    @property
    def listening(self) -> bool:
        return self._reader is not None and self._reader.is_alive()

    def set_listening(self, on: bool) -> None:
        """マイクを開ける・閉じる。

        閉じるときは**聞く側のプロセスごと終わらせる**（無視するだけだと、録り続けることになる）。
        """
        if on and not self.listening:
            self._stop.clear()
            self.ears = self._make_ears()
            self._reader = threading.Thread(target=self._listen, args=(self._loop,), daemon=True)
            self._reader.start()
            log.info("マイクを開けました")
        elif not on and self.listening:
            self._stop.set()
            if self.ears is not None:
                self.ears.__exit__(None, None, None)
            self._reader = None
            self._pending = None
            log.info("マイクを閉じました")

    async def run(self, listening: bool = False) -> None:
        """聞こえたものを、順に捌く。**既定ではマイクを開けない**（Slack から入れる）。"""
        self._loop = asyncio.get_running_loop()
        if listening:
            self.set_listening(True)
        try:
            while True:
                text = await self.heard.get()
                try:
                    await self.handle(text)
                except Exception:
                    log.exception("聞こえたことを捌けませんでした: %r", text[:80])
        finally:
            self.set_listening(False)

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

    # 聞こえた1件

    async def handle(self, text: str, now: datetime | None = None) -> None:
        """聞こえた1件を捌く。呼ばれていなければ、何もしない。"""
        if await self._confirmed(text):
            return
        if not wake.called(text):
            return
        asked = wake.request(text)
        log.info("呼ばれました: %r", asked[:80])
        if not asked:
            # 名前だけ呼ばれた。相槌だけ返す
            await self.say(NODDED)
            return
        self._write(f"{asked}")
        if any(word in asked for word in FRESH_WORDS):
            self.codex.forget()
            await self.say(FRESH)
            return
        answer = fast.answer(asked, self.held, now or datetime.now())
        if answer is not None:
            await self.say(answer)
            await self._follow_up(asked, answer)
            return
        await self.slow(asked)

    async def slow(self, asked: str) -> None:
        """遅い道。相槌を返してから Codex に相談する。"""
        await self.say(THINKING if think.wants_deep(asked) else NODDED)
        turn = await self._think(asked)
        if turn.failed:
            log.info("相談できませんでした: %s", turn.failed)
            await self.say(turn.failed if turn.failed.endswith("。") else f"{turn.failed}。")
            return
        await self.say(turn.text)
        if turn.ask is not None:
            await self._confirm(turn.ask)

    async def _follow_up(self, asked: str, answered: str) -> None:
        """速い道で即答したあと、付け足すことがあれば1文だけ追いかけて喋る。

        毎回2回喋るとうるさいので、**Codex が `🗣 ひとこと:` を書いたときだけ**（docs/voice.md の3節）。
        時刻のように付け足す余地のない問いでは、往復そのものをしない。
        """
        if fast.closed(asked):
            return
        turn = await self._think(think.FOLLOW_UP.format(
            asked=asked, answered=answered, nothing=think.NOTHING, aside=think.ASIDE_MARK))
        if turn.aside and turn.aside != think.NOTHING:
            await self.say(turn.aside)

    async def _think(self, text: str) -> think.Turn:
        """Codex に話しかける。止まるので、別のスレッドに出す。"""
        turn = await asyncio.get_running_loop().run_in_executor(None, self.codex.ask, text)
        if turn.text:
            self._write(turn.text, who="Kei")
        return turn

    # 依頼（読み上げて確認してから渡す）

    async def _confirm(self, pending: think.Ask) -> None:
        self._pending, self._pending_at = pending, time.time()
        await self.say(pending.spoken)

    async def _confirmed(self, text: str) -> bool:
        """確認の返事なら、それとして捌いて True。そうでなければ False。"""
        pending = self._pending
        if pending is None:
            return False
        if time.time() - self._pending_at > CONFIRM_SECONDS:
            self._pending = None
            return False
        self._pending = None
        said = text.strip().lower()
        if any(word in said for word in NO):
            await self.say(DROPPED)
            return True
        if not any(word in said for word in YES):
            # 聞き間違いで作業が動き出すより、もう一度言ってもらう方がよい
            await self.say(UNSURE)
            return True
        self._write(f"{pending.theme} に「{pending.text}」を渡した", who="Kei")
        await asyncio.get_running_loop().run_in_executor(
            None, asks.write_ask, self.config, pending.theme, pending.text)
        await self.say(HANDED)
        return True

    # 口と記録

    async def say(self, text: str) -> None:
        """喋る。合成と再生で止まるので、別のスレッドに出す。"""
        if text.strip():
            await asyncio.get_running_loop().run_in_executor(None, self.mouth.say, text)

    def _write(self, text: str, who: str = "依頼者") -> None:
        journal.append(self.config.overview_dir, who, text)
