"""作業中の見せ方。Slack の AI アプリ（Agent）向けの API で、手順と返事を流して見せる。"""

from __future__ import annotations

import logging

from kei_agent.slack_text import split_text

log = logging.getLogger(__name__)

# 作業の手順（task_update）の見出しと補足の長さ
TASK_TITLE_LIMIT = 150
TASK_DETAILS_LIMIT = 300
# 作業中に状態欄に出す文言
THINKING_TEXT = "考え中…"


class ThreadUI:
    """作業中の見せ方。Slack の AI アプリ（Agent）向けの API を使う。

    - `agents.sessions.setStatus`: スレッドの状態を切り替える。processing（作業中。Slack が「Working...」を出す）、
      active（次の依頼待ち）、suspended（依頼者の返事待ち）のどれか。自由な文章は出せない
    - `assistant.threads.setStatus`: 状態欄の文言を自分で決める。作業中は「考え中…」を出す
      （返事を投稿すると Slack が自分で消す）
    - `chat.startStream` / `appendStream` / `stopStream`: 返事を流して見せる。道具を使うたびに作業の手順
      （task_update）を1行ずつ足し、最後にまとめを本文として出す

    途中で Claude が書いた独り言は、本文には出さず、次の手順の補足にする。本文に流すと、
    最後のまとめと同じ話が2回並ぶ。

    どちらもワークスペースや App の設定によっては使えないので、失敗したら黙って
    今までどおりの投稿に戻す（1回目だけログに残す）。
    """

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
        self.task: dict | None = None  # いま実行中の手順
        self.task_count = 0
        self.narration = ""  # 次の手順に添える独り言

    async def start(self) -> None:
        await self._status("processing")

    async def activity(self, activity: str) -> None:
        """道具を呼ぶたびに呼ばれる。前の手順を完了にし、新しい手順を実行中として足す。"""
        chunks = self._complete_task()
        self.task_count += 1
        self.task = {"type": "task_update", "id": f"step-{self.task_count}",
                     "title": activity[:TASK_TITLE_LIMIT], "status": "in_progress"}
        if self.narration:
            self.task["details"] = self.narration[:TASK_DETAILS_LIMIT]
            self.narration = ""
        await self._stream(chunks + [self.task])

    async def text(self, chunk: str) -> None:
        """Claude が書いた文章。最後のまとめは finish() で本文にするので、ここでは次の手順の補足として取っておく。"""
        if chunk.strip():
            self.narration = chunk.strip()

    async def finish(self, answer: str, awaiting: bool = False) -> bool:
        """手順を閉じてまとめを本文に出し、スレッドを次の依頼待ち（返事待ちなら suspended）に戻す。

        流して見せられていれば True（結果をもう一度投稿しない）。
        """
        chunks = self._complete_task()
        if chunks:
            await self._stream(chunks)
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
        await self._status("processing")

    def _complete_task(self) -> list[dict]:
        if self.task is None:
            return []
        # details は始めたときに送ってある。Slack は同じ id の details を足していくので、完了では送り直さない
        done = {k: v for k, v in self.task.items() if k != "details"} | {"status": "complete"}
        self.task = None
        return [done]

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
                    task_display_mode="timeline",
                    **content,
                )
                self.stream_ts = resp["ts"]
            else:
                await self.slack.chat_appendStream(channel=self.channel, ts=self.stream_ts, **content)
        except Exception:
            log.warning("返事を流して見せられないので、まとめて投稿します", exc_info=True)
            self.stream_ok = False

    async def _thinking(self) -> None:
        if not self.thinking_ok:
            return
        try:
            await self.slack.assistant_threads_setStatus(
                channel_id=self.channel, thread_ts=self.thread_ts, status=THINKING_TEXT)
        except Exception:
            # scope（assistant:write）がないと使えない。そのときは Slack 任せの表示のまま
            log.info("状態欄の文言を出せません", exc_info=True)
            self.thinking_ok = False

    async def _status(self, status: str) -> None:
        if status == "processing":
            await self._thinking()
        if not self.status_ok:
            return
        try:
            await self.slack.agents_sessions_setStatus(channel_id=self.channel, thread_ts=self.thread_ts, status=status)
        except Exception:
            # App の「Agent experience」が有効でないと使えない
            log.info("スレッドのステータスを出せません", exc_info=True)
            self.status_ok = False

