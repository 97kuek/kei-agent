"""机の上の音声対話（第0段階）。

Aqua Voice などで喋った文を受け取り、Codex に渡し、返事を VOICEVOX で読み上げる。
決まったことは Slack に残し、依頼は読み上げて確認してから Kei Agent に渡す。

使い方:
    uv run kei-agent-voice            # VOICEVOX アプリを立ち上げておく
    「新しく話そう」             # 会話を切る
    「じっくり考えて」を含める   # その回だけ深く考えさせる
    Enter だけ                   # 読み上げを止める
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from kei_agent.config import Config, load_config
from kei_agent_voice import bridge, journal, markers
from kei_agent_voice.brain import Codex, today
from kei_agent_voice.speech import Speaker, Voicevox

log = logging.getLogger(__name__)

RESET_WORDS = ("新しく話そう", "新しく話して", "話を切って", "リセット")
YES_WORDS = ("いいよ", "はい", "お願い", "うん", "やって", "それで", "オッケー", "ok", "OK")
# 「いい」は「いいよ」と「いいです（やらない）」の両方に使われるので入れない
NO_WORDS = ("やめて", "やめ", "いや", "違う", "ちがう", "まだ", "だめ", "キャンセル", "しないで")
PROMPT = "あなた> "
THINKING = "考え中…"


@dataclass
class Pending:
    """読み上げて確認している最中の依頼。"""
    theme: str
    text: str


@dataclass
class Session:
    """声の会話1回ぶんの状態。"""
    config: Config
    codex: Codex
    speaker: Speaker
    theme: str = ""
    pending: Pending | None = None
    started_at: float = field(default_factory=time.time)
    watcher: bridge.Watcher | None = None

    def __post_init__(self) -> None:
        self.watcher = self.watcher or bridge.Watcher(self.config, self.started_at)

    # 1行受け取って、返す言葉を作る

    def handle(self, line: str, now: float | None = None) -> str:
        text = line.strip()
        if not text:
            self.speaker.stop()
            return ""
        if any(word in text for word in RESET_WORDS):
            self.codex.forget()
            return "わかった。新しく話そう。"
        if self.pending is not None:
            return self.answer_pending(text)
        return self.talk(text, now)

    def answer_pending(self, text: str) -> str:
        """依頼を出す前の「いい？」への返事。"""
        pending, self.pending = self.pending, None
        assert pending is not None
        if any(word in text for word in NO_WORDS):
            return "わかった、やめておく。"
        if any(word in text for word in YES_WORDS):
            bridge.send_request(self.config, pending.theme, pending.text)
            return f"{pending.theme} に渡したよ。終わったら知らせるね。"
        return "はっきり分からなかったから、やめておく。もう一度言って。"

    def talk(self, text: str, now: float | None = None) -> str:
        day = today(now)
        journal.append(self.config, day, "依頼者", text, now)
        turn = self.codex.ask(text, day, on_text=self.say_reply)
        if turn.failed:
            return self.limit_message(turn.limit_reset)
        reply = markers.parse(turn.text)
        journal.append(self.config, day, "Kei（声）", turn.text, now)
        if reply.theme:
            self.theme = reply.theme
        for decision in reply.decisions:
            if self.theme:
                bridge.send_note(self.config, self.theme, decision)
        if reply.requests:
            return self.confirm(reply.requests[0])
        return ""

    def confirm(self, request: str) -> str:
        if not self.theme:
            return "どのテーマの話か分からないから、先に教えて。"
        self.pending = Pending(self.theme, request)
        return f"{self.theme} に「{request}」と頼むよ。いい？"

    def limit_message(self, reset: str | None) -> str:
        when = f"{reset} ごろ" if reset else "しばらくして"
        return f"Codex の上限に当たったみたい。{when}に戻るから、それまでは Slack の Kei Agent に頼んでね。"

    # 声に出す

    def say_reply(self, chunk: str) -> None:
        """Codex の返事が届いたら、目印の行を外して読み上げる。"""
        for sentence in markers.sentences(markers.parse(chunk).spoken):
            self.speaker.say(sentence)

    def say(self, text: str) -> None:
        if not text:
            return
        print(f"Kei> {text}")
        for sentence in markers.sentences(text):
            self.speaker.say(sentence)

    def announce_finished(self) -> None:
        """Kei Agent に渡した作業が終わっていたら、一言だけ伝える。"""
        assert self.watcher is not None
        for done in self.watcher.finished():
            self.say(done.message)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    instructions = _instructions(config)
    state = config.state_dir / "voice"
    codex = Codex(cwd=config.research_root, state_dir=state, instructions=instructions)
    engine = Voicevox()
    speaker = Speaker(engine)
    session = Session(config=config, codex=codex, speaker=speaker)
    if not speaker.enabled:
        print("VOICEVOX につながらないので、声なしで続けます（アプリを立ち上げると読み上げます）")
    print("机の上の Kei です。喋ってください（Enter だけで読み上げを止める、"
          "「新しく話そう」で会話を切る、Ctrl-C で終わり）")
    try:
        while True:
            session.announce_finished()
            try:
                line = input(PROMPT)
            except EOFError:
                # 入力が尽きたら、言いかけのことを言い終えてから終わる
                speaker.wait()
                break
            if line.strip():
                print(THINKING)
            session.say(session.handle(line))
    except KeyboardInterrupt:
        print()
    finally:
        speaker.close()


def _instructions(config: Config) -> str:
    path = config.repo_root / "prompts" / "voice.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


if __name__ == "__main__":
    main()
