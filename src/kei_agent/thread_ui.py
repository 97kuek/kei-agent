"""作業中の見せ方。Slack の AI アプリ（Agent）向けの API を使う。

- `agents.sessions.setStatus`: スレッドの状態。processing（作業中。Slack が「Working...」を出す）、
  active（次の依頼待ち）、suspended（依頼者の返事待ち）のどれか
- `assistant.threads.setStatus`: 入力欄の下に出る1行。固定の利用者向け状態だけを出す
- `chat.startStream` / `appendStream` / `stopStream`: 返事を流して見せる。出すのは最後のまとめだけ

作業の手順を返事の中に1行ずつ並べると、長い作業ほどメッセージが伸びて、結論を読むのにスクロールが要る。
そこで経過は入力欄の下の1行にとどめ、スレッドにはまとめだけを残す。

どの API もワークスペースや App の設定によっては使えないので、失敗したら黙って
今までどおりの投稿に戻す（1回目だけログに残す）。
"""

from __future__ import annotations

import logging

from kei_agent.slack_text import split_text

log = logging.getLogger(__name__)

# 入力欄の下に出す文言の長さ
STATUS_LIMIT = 100
# 何もしていないときの文言
THINKING_TEXT = "考え中…"
WAITING_JOB_TEXT = "ジョブの結果を待っている…"
_PROGRESS_TEXTS = ("調べている…", "作業している…", "まとめている…")


class ThreadUI:
    def __init__(self, slack, channel: str, thread_ts: str, team_id: str = "", user_id: str = ""):
        self.slack = slack
        self.channel = channel
        self.thread_ts = thread_ts
        self.team_id = team_id
        self.user_id = user_id
        self.status_ok = True
        self.thinking_ok = True
        self.stream_ok = True
        self.stream_ts: str | None = None
        # いま何をしているか（入力欄の下に出しているもの）
        self.doing = ""

    async def start(self) -> None:
        await self._status("processing")

    async def activity(self, activity: str) -> None:
        """道具の詳細を出さず、固定の利用者向け進捗だけを見せる。"""
        lowered = activity.lower()
        if any(word in lowered for word in ("read", "search", "fetch", "glob", "grep", "調べ", "確認")):
            shown = _PROGRESS_TEXTS[0]
        elif any(word in lowered for word in ("summar", "まとめ", "answer", "返答")):
            shown = _PROGRESS_TEXTS[2]
        else:
            shown = _PROGRESS_TEXTS[1]
        await self._thinking(shown)

    async def text(self, chunk: str) -> None:
        """モデルの途中 text は本文にも status にも表示しない。"""
        if chunk.strip():
            await self._thinking(_PROGRESS_TEXTS[2])

    async def finish(self, answer: str, awaiting: bool = False) -> bool:
        """まとめを本文に出し、スレッドを次の依頼待ち（返事待ちなら suspended）に戻す。

        流して見せられていれば True（結果をもう一度投稿しない）。
        """
        for piece in split_text(answer) if answer.strip() else []:
            await self._stream([{"type": "markdown_text", "text": piece}])
        streamed = False
        if self.stream_ts is not None:
            try:
                await self.slack.chat_stopStream(channel=self.channel, ts=self.stream_ts)
                streamed = self.stream_ok and bool(answer.strip())
            except Exception:
                log.warning("流して見せた返事を終われません", exc_info=True)
        await self._status("suspended" if awaiting else "active")
        return streamed

    async def keep_working(self) -> None:
        """ジョブが走っている間は、返事を終えたあとも作業中に見せておく。"""
        await self._status("processing", WAITING_JOB_TEXT)

    async def _stream(self, chunks: list[dict]) -> None:
        # 始めたときと違う形（chunks と markdown_text 引数）を混ぜると streaming_mode_mismatch で断られるので、
        # 本文も含めて全部 chunks で送る
        if not self.stream_ok:
            return
        content = {"chunks": chunks}
        try:
            if self.stream_ts is None:
                resp = await self.slack.chat_startStream(
                    channel=self.channel,
                    thread_ts=self.thread_ts,
                    recipient_team_id=self.team_id or None,
                    recipient_user_id=self.user_id or None,
                    **content,
                )
                self.stream_ts = resp["ts"]
            else:
                await self.slack.chat_appendStream(channel=self.channel, ts=self.stream_ts, **content)
        except Exception:
            log.warning("返事を流して見せられないので、まとめて投稿します", exc_info=True)
            self.stream_ok = False

    async def _thinking(self, text: str = THINKING_TEXT) -> None:
        text = text.strip()[:STATUS_LIMIT] or THINKING_TEXT
        if not self.thinking_ok or text == self.doing:
            return
        self.doing = text
        try:
            await self.slack.assistant_threads_setStatus(
                channel_id=self.channel, thread_ts=self.thread_ts, status=text)
        except Exception:
            # scope（assistant:write）がないと使えない。そのときは Slack 任せの表示のまま
            log.info("状態欄の文言を出せません", exc_info=True)
            self.thinking_ok = False

    async def _status(self, status: str, doing: str = THINKING_TEXT) -> None:
        if status == "processing":
            await self._thinking(doing)
        if not self.status_ok:
            return
        try:
            await self.slack.agents_sessions_setStatus(channel_id=self.channel, thread_ts=self.thread_ts, status=status)
        except Exception:
            # App の「Agent experience」が有効でないと使えない
            log.info("スレッドのステータスを出せません", exc_info=True)
            self.status_ok = False
