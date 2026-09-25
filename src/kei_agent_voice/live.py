"""声で話す相手（OpenAI Realtime API）。音をそのままやりとりする（docs/architecture.md の「声のレイヤ」）。

**`gpt-live-1` ではなく Realtime API を使う。** 名前は似ているが別物で、こちらが用途に合う。

| | Realtime API（採用） | GPT-Live（`gpt-live-1`） |
|---|---|---|
| 課金 | **Response を作ったときだけ**。黙っていれば、繋いだままでもかからない | **秒単位で $0.05/分。無音でもかかる** |
| 道具（予定・締切・研究の中身） | セッションの中で呼べる | **別に backend を立てて委譲する必要がある** |
| 喋り終わりの合図 | `response.output_audio.done` がある | **無い** |

マイクを開けている時間がそのまま課金になる作りだと、開けっぱなしが怖くて使えない。

## 流れ

1. `wss://api.openai.com/v1/realtime?model=...` に繋ぐ（`Authorization: Bearer` だけ。**ベータヘッダは付けない**）
2. `session.update` で人格・声・道具・ターンの検出を**1回だけ**入れる
3. マイクの音を `input_audio_buffer.append` に base64 で流し続ける
4. `response.output_audio.delta` で返ってくる音を鳴らす
5. `input_audio_buffer.speech_started`（依頼者が喋り出した）で**鳴っている音を捨て、どこまで聞かれたかを伝える**

## 踏んだ罠（調べて分かったこと）

- `modalities` は無い。**`output_modalities`**。そして `["text", "audio"]` は**指定できない**。
  音声で喋らせながら文字も欲しいので、文字は `response.output_audio_transcript.delta` から取る
- PCM は **24000Hz のみ**。チャンクの**バイト数は偶数**でなければならない（16bit の途中で切ると壊れる）
- `conversation.item.truncate` の `audio_end_ms` が**実際の長さを超えるとエラー**。少なめに丸める
- **セッションの途中で人格や道具を書き換えない**（会話の先頭にあるので、以降のキャッシュが全部外れる）
- セッションは **60分で切れる**。切れたら `run` の輪が黙って繋ぎ直す（履歴は消える）
- `instructions` を入れないと**英語の人格**が入る（サーバー既定）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import aiohttp

from kei_agent_voice import audio
from kei_agent_voice.audio import Unavailable
from kei_agent_voice.tools import DEFINITIONS, Tools

log = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/realtime"
KEY_ENV = "OPENAI_API_KEY"
VOICE_ENV = "KEI_AGENT_REALTIME_VOICE"
# 音声会話は通常 agent の recipe と分離し、依頼者が選んだ mini に固定する。
DEFAULT_MODEL = "gpt-realtime-2.1-mini"
# 公式が質のために薦めるのは marin と cedar。cedar のほうが低い声（依頼者の分身なので）
DEFAULT_VOICE = "cedar"
# 話し終わったかを意味で判断する。`high` は最大2秒待ち（`low` は8秒でもっさりする）
EAGERNESS = "high"
# 鳴らした長さの見積りを、この分だけ少なめに言う（超えるとサーバーが断る）
TRUNCATE_MARGIN_MS = 100
# 繋ぎ直すまでの待ち。切れるたびに倍にして、上限で止める
BACKOFF_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
# これより長く繋がっていたら、待ちを最初に戻す（60分で切れるのはふつうのこと）
STABLE_SECONDS = 60.0
# 鍵を断られた。繋ぎ直しても同じなので諦める
REJECTED = (401, 403)
# 通知1件にかけてよい時間（繋がらない・終わらないまま次の通知を待たせない）
SAY_ONCE_SECONDS = 60.0

INSTRUCTIONS = """# Role and Objective
あなたは「Kei」。依頼者本人の分身で、机の上のロボットの声として話す。
研究・授業・仕事を抱えた依頼者の相談相手。結論から短く話す。

# Personality and Tone
依頼者と対等な友だちのように話す。敬語は使わない（「〜だよ」「〜だね」「〜かな」）。
励ましすぎない。わざとらしい共感を入れない。

# Language
- 返答は常に**日本語**。
- 短い相づち、フィラー、単発の英単語（論文名・API 名・テーマ名）で言語を切り替えない。
- 依頼者が明示的に頼んだときだけ、他の言語に切り替える。

# Verbosity
- 1回の返事は**2文以内**。長くなるときは「詳しく話す？」と聞いてから。
- 箇条書きにしない。声なので、順番に喋る。

# Tools
- 予定・授業・会議・締切を聞かれたら、**必ず `get_schedule` を呼ぶ**。記憶で答えない。
  「明日」「今週」「今日」のどれかを引数で渡す。分からなければ today。
- Kei Agent が何をしているか聞かれたら `get_status` を呼ぶ。
- 研究・授業・仕事の**中身**（テーマで何を確かめているか、結果がどうだったか）を聞かれたら
  `ask_agent` を呼ぶ。数秒かかるので、呼ぶ前に「ちょっと見てみるね」と一言だけ言う。
- 作業を頼まれたら `propose_request` を呼び、**返ってきた文をそのまま読み上げて確認する**。
  依頼者が「いいよ」と言ってから `send_request` を呼ぶ。確認していないのに送らない。

# Unclear Audio
- 聞き取れないときは推測で埋めず、短く聞き返す。
- 固有名詞は崩れて届く（`amr-query` が「アムルクエリー」など）。心当たりのテーマ名に読み替える。

# お知らせ
「（お知らせ）」で始まる入力は、Kei Agent の本体から来た出来事。
依頼者に向けて、そのまま一言で伝える。聞き返さない。
"""


def _session(voice: str, tools: list[dict]) -> dict:
    """`session.update` に渡すもの。**繋いだ直後に1回だけ**送る。"""
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": INSTRUCTIONS,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": audio.RATE},
                    # ノートの内蔵マイクなので far_field。VAD の誤検出が減る
                    "noise_reduction": {"type": "far_field"},
                    "transcription": {"model": "gpt-transcribe", "language": "ja"},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": EAGERNESS,
                        "create_response": True,
                        # 依頼者が喋り出したら、こちらの返事を勝手に止めてくれる
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": audio.RATE},
                    "voice": voice,
                },
            },
            "tools": tools,
            "tool_choice": "auto",
            # 履歴を毎回送るので、長い会話は後半が高い。余裕を持って切り詰める
            "truncation": {"type": "retention_ratio", "retention_ratio": 0.8},
        },
    }


def _notice(text: str) -> dict:
    """本体から来た知らせを、会話に1件足す形。"""
    return {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": f"（お知らせ）{text}"}]},
    }


async def _events(ws) -> AsyncIterator[dict]:
    """届いたイベント。文字で、読めるものだけ。"""
    async for message in ws:
        if message.type is not aiohttp.WSMsgType.TEXT:
            continue
        try:
            event = json.loads(message.data)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _calls(event: dict) -> list[dict]:
    outputs = ((event.get("response") or {}).get("output") or [])
    return [o for o in outputs if isinstance(o, dict) and o.get("type") == "function_call"]


class Live:
    """Realtime API との1本の繋がり。`run` を回している間だけ喋る。"""

    def __init__(self, tools: Tools, key: str = "", voice: str = "",
                 env: dict | None = None):
        env = os.environ if env is None else env
        self.tools = tools
        self.key = key or env.get(KEY_ENV, "")
        self.model = DEFAULT_MODEL
        self.voice = voice or env.get(VOICE_ENV) or DEFAULT_VOICE
        self.speaker = audio.Speaker()
        # いま鳴らしている返事（割り込みのときに「どこまで聞かれたか」を伝える相手）
        self.speaking_item = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._notices: asyncio.Queue[str] = asyncio.Queue()
        # 返事を作っていないあいだだけ立つ。知らせはこれを待ってから頼む（重ねて頼むと断られる）
        self._idle = asyncio.Event()
        self._idle.set()
        # 道具を答えている途中のもの（受け取りを止めないよう、別に走らせる）
        self._tool_tasks: set[asyncio.Task] = set()

    # 繋ぐ

    def _require_key(self) -> None:
        if not self.key:
            raise Unavailable(f"{KEY_ENV} が置かれていません（deploy/README.md を見てください）")

    @asynccontextmanager
    async def _connect(self, tools: list[dict]):
        """繋いで、人格・声・道具を**1回だけ**入れる。"""
        headers = {"Authorization": f"Bearer {self.key}"}
        async with (
            aiohttp.ClientSession(headers=headers) as http,
            http.ws_connect(f"{URL}?model={self.model}", heartbeat=20) as ws,
        ):
            await ws.send_json(_session(self.voice, tools))
            yield ws

    async def run(self, on_said: Callable[[str, str], None] | None = None) -> None:
        """繋いで、マイクと口を回す。切れたら**間を空けて繋ぎ直す**（60分で切れる）。"""
        self._require_key()
        delay = BACKOFF_SECONDS
        while True:
            began = time.monotonic()
            try:
                await self._once(on_said)
                log.info("声の繋がりが切れました。繋ぎ直します")
            except aiohttp.WSServerHandshakeError as e:
                if e.status in REJECTED:
                    raise Unavailable(f"鍵を断られました（{e.status}）。{KEY_ENV} を確かめてください") from e
                log.warning("繋ぎ直します: %s", e)
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                log.warning("繋ぎ直します: %s", e)
            if time.monotonic() - began >= STABLE_SECONDS:
                delay = BACKOFF_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_SECONDS)

    async def _once(self, on_said: Callable[[str, str], None] | None) -> None:
        async with self._connect(DEFINITIONS) as ws:
            self._ws = ws
            self._idle.set()
            log.info("声で繋がりました（%s / %s）", self.model, self.voice)
            mic = asyncio.create_task(self._send_microphone())
            notices = asyncio.create_task(self._send_notices())
            receiving = asyncio.create_task(self._receive(ws, on_said))
            try:
                await asyncio.wait({mic, receiving}, return_when=asyncio.FIRST_COMPLETED)
                if mic.done():
                    # マイクが先に止まった。聞いているふりを続けない
                    mic.result()
                    raise Unavailable("マイクが閉じました")
                receiving.result()
            finally:
                tasks = [mic, notices, receiving, *self._tool_tasks]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.speaker.stop()
                self._ws = None

    async def say_once(self, text: str,
                       on_said: Callable[[str, str], None] | None = None) -> None:
        """マイクを開かず、通知1件を読み上げて接続を閉じる。"""
        self._require_key()
        # 途中で「聞く」に切り替わっても、会話の接続と再生を上書きしない。
        speaker = audio.Speaker()
        transcript = ""
        try:
            async with asyncio.timeout(SAY_ONCE_SECONDS), self._connect([]) as ws:
                await ws.send_json(_notice(text))
                await ws.send_json({"type": "response.create"})
                async for event in _events(ws):
                    kind = event.get("type", "")
                    if kind == "response.output_audio.delta":
                        speaker.write(base64.b64decode(event.get("delta") or ""))
                    elif kind == "response.output_audio_transcript.delta":
                        transcript += str(event.get("delta") or "")
                    elif kind == "response.output_audio_transcript.done":
                        transcript = str(event.get("transcript") or transcript)
                    elif kind == "error":
                        raise Unavailable("通知の音声応答が API に拒否されました")
                    elif kind in {"response.output_audio.done", "response.done"}:
                        response = event.get("response") or {}
                        if response.get("status") in {"failed", "cancelled", "incomplete"}:
                            raise Unavailable("通知の音声応答が完了しませんでした")
                        # done は生成の完了。手元のバッファを鳴らし終えてから閉じる。
                        await speaker.wait_until_done()
                        if on_said:
                            on_said("Kei", transcript.strip() or text)
                        return
                raise Unavailable("通知の音声応答が完了する前に接続が閉じました")
        except TimeoutError as e:
            raise Unavailable("通知の音声応答が時間内に終わりませんでした") from e
        finally:
            speaker.stop()

    # 送る

    async def _send_microphone(self) -> None:
        """マイクの音を流し続ける。読むところが止まるので、別のスレッドで読む。"""
        loop = asyncio.get_running_loop()
        with audio.Microphone() as mic:
            chunks = mic.chunks()
            while True:
                chunk = await loop.run_in_executor(None, lambda: next(chunks, b""))
                if not chunk or self._ws is None:
                    return
                # 16bit の途中で切れていると壊れる。端数は落とす
                if len(chunk) % 2:
                    chunk = chunk[:-1]
                await self._ws.send_json({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                })

    def announce(self, text: str) -> None:
        """本体から来た知らせを、この声で喋らせる（投げっぱなし）。"""
        self._notices.put_nowait(text)

    async def _send_notices(self) -> None:
        while True:
            text = await self._notices.get()
            # 返事の途中で頼むと断られる。喋り終わってから
            await self._idle.wait()
            if self._ws is None:
                continue
            self._idle.clear()
            await self._ws.send_json(_notice(text))
            await self._ws.send_json({"type": "response.create"})

    # 受ける

    async def _receive(self, ws, on_said: Callable[[str, str], None] | None) -> None:
        async for event in _events(ws):
            kind = event.get("type", "")
            if kind == "response.output_audio.delta":
                item = str(event.get("item_id") or self.speaking_item)
                if item != self.speaking_item:
                    # 割り込みで伝える長さは、返事の中の位置。返事ごとに数え直す
                    self.speaker.begin_item()
                    self.speaking_item = item
                self.speaker.write(base64.b64decode(event.get("delta") or ""))
            elif kind == "input_audio_buffer.speech_started":
                await self._interrupt(ws)
            elif kind == "response.output_audio_transcript.done":
                text = str(event.get("transcript") or "").strip()
                if text and on_said:
                    on_said("Kei", text)
            elif kind == "conversation.item.input_audio_transcription.completed":
                text = str(event.get("transcript") or "").strip()
                if text and on_said:
                    on_said("依頼者", text)
            elif kind == "response.created":
                self._idle.clear()
            elif kind == "response.done":
                self._idle.set()
                if _calls(event):
                    self._answer_later(ws, event)
            elif kind == "error":
                # 頼んだ返事が断られると response.done は来ない。知らせを止めたままにしない
                self._idle.set()
                log.warning("声のやりとりで断られました: %s", json.dumps(
                    event.get("error") or event, ensure_ascii=False)[:300])

    async def _interrupt(self, ws) -> None:
        """依頼者が喋り出した。鳴っている音を捨てて、どこまで聞かれたかを伝える。

        `interrupt_response` を立ててあるので、返事を止めるのはサーバーがやる。
        こちらの仕事は**再生を止めることと、聞かれた長さを伝えること**だけ。
        """
        played = self.speaker.stop()
        item, self.speaking_item = self.speaking_item, ""
        if not item or played <= 0:
            return
        await ws.send_json({
            "type": "conversation.item.truncate",
            "item_id": item,
            "content_index": 0,
            # 超えるとサーバーが断るので、少なめに言う
            "audio_end_ms": max(played - TRUNCATE_MARGIN_MS, 0),
        })

    def _answer_later(self, ws, event: dict) -> None:
        """道具の答えは別に走らせる（ask_agent は数秒かかる。そのあいだも割り込みを受ける）。"""
        task = asyncio.create_task(self._answer_tools(ws, event))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_done)

    def _tool_done(self, task: asyncio.Task) -> None:
        self._tool_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("道具の答えを返せませんでした: %s", task.exception())

    async def _answer_tools(self, ws, event: dict) -> None:
        """道具を呼ばれていたら、答えを返して続きを喋らせる。"""
        calls = _calls(event)
        if not calls:
            return
        for call in calls:
            name = str(call.get("name") or "")
            try:
                arguments = json.loads(call.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            log.info("道具を呼ばれました: %s %s", name, json.dumps(arguments, ensure_ascii=False)[:120])
            answer = await self.tools.call(name, arguments)
            await ws.send_json({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output",
                         "call_id": call.get("call_id"),
                         "output": json.dumps({"text": answer}, ensure_ascii=False)},
            })
        # これを送らないとモデルは黙ったまま
        self._idle.clear()
        await ws.send_json({"type": "response.create"})

    def close(self) -> None:
        """聞くのをやめる。溜まった知らせも捨てる（次に繋いだときに古い知らせを喋らない）。"""
        while not self._notices.empty():
            self._notices.get_nowait()
        self.speaker.close()
