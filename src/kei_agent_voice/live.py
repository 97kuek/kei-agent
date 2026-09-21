"""声で話す相手（OpenAI Realtime API）。音をそのままやりとりする（docs/voice.md の3節）。

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
from collections.abc import Callable

import aiohttp

from kei_agent_voice import audio
from kei_agent_voice.tools import Tools

log = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/realtime"
KEY_ENV = "OPENAI_API_KEY"
MODEL_ENV = "KEI_AGENT_REALTIME_MODEL"
VOICE_ENV = "KEI_AGENT_REALTIME_VOICE"
# 廃止予定が無いもの。`mini` に落とすと安いが、道具の呼び分けが弱くなると公式が書いている
DEFAULT_MODEL = "gpt-realtime-2.1"
# 公式が質のために薦めるのは marin と cedar。cedar のほうが低い声（依頼者の分身なので）
DEFAULT_VOICE = "cedar"
# 話し終わったかを意味で判断する。`high` は最大2秒待ち（`low` は8秒でもっさりする）
EAGERNESS = "high"
# 鳴らした長さの見積りを、この分だけ少なめに言う（超えるとサーバーが断る）
TRUNCATE_MARGIN_MS = 100

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
  `ask_research` を呼ぶ。数秒かかるので、呼ぶ前に「ちょっと見てみるね」と一言だけ言う。
- 作業を頼まれたら `propose_request` を呼び、**返ってきた文をそのまま読み上げて確認する**。
  依頼者が「いいよ」と言ってから `send_request` を呼ぶ。確認していないのに送らない。

# Unclear Audio
- 聞き取れないときは推測で埋めず、短く聞き返す。
- 固有名詞は崩れて届く（`amr-query` が「アムルクエリー」など）。心当たりのテーマ名に読み替える。

# お知らせ
「（お知らせ）」で始まる入力は、Kei Agent の本体から来た出来事。
依頼者に向けて、そのまま一言で伝える。聞き返さない。
"""


class Unavailable(Exception):
    """繋げない（鍵が無い、断られた）。"""


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
            # 声の返事なので、深く考えさせない
            "reasoning": {"effort": "low"},
            # 履歴を毎回送るので、長い会話は後半が高い。余裕を持って切り詰める
            "truncation": {"type": "retention_ratio", "retention_ratio": 0.8},
        },
    }


class Live:
    """Realtime API との1本の繋がり。`run` を回している間だけ喋る。"""

    def __init__(self, tools: Tools, key: str = "", model: str = "", voice: str = "",
                 env: dict | None = None):
        env = os.environ if env is None else env
        self.tools = tools
        self.key = key or env.get(KEY_ENV, "")
        self.model = model or env.get(MODEL_ENV) or DEFAULT_MODEL
        self.voice = voice or env.get(VOICE_ENV) or DEFAULT_VOICE
        self.speaker = audio.Speaker()
        # いま鳴らしている返事（割り込みのときに「どこまで聞かれたか」を伝える相手）
        self.speaking_item = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._notices: asyncio.Queue[str] = asyncio.Queue()

    # 繋ぐ

    async def run(self, on_said: Callable[[str, str], None] | None = None) -> None:
        """繋いで、マイクと口を回す。切れたら**黙って繋ぎ直す**（60分で切れる）。"""
        if not self.key:
            raise Unavailable(f"{KEY_ENV} が置かれていません（deploy/README.md を見てください）")
        while True:
            try:
                await self._once(on_said)
            except Unavailable:
                raise
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                log.warning("繋ぎ直します: %s", e)
                await asyncio.sleep(2)

    async def _once(self, on_said: Callable[[str, str], None] | None) -> None:
        headers = {"Authorization": f"Bearer {self.key}"}
        async with (
            aiohttp.ClientSession(headers=headers) as http,
            http.ws_connect(f"{URL}?model={self.model}", heartbeat=20) as ws,
        ):
            self._ws = ws
            await ws.send_json(_session(self.voice, self.tools_definitions))
            log.info("声で繋がりました（%s / %s）", self.model, self.voice)
            mic = asyncio.create_task(self._send_microphone())
            notices = asyncio.create_task(self._send_notices())
            try:
                await self._receive(ws, on_said)
            finally:
                for task in (mic, notices):
                    task.cancel()
                self.speaker.stop()
                self._ws = None

    @property
    def tools_definitions(self) -> list[dict]:
        from kei_agent_voice.tools import DEFINITIONS
        return DEFINITIONS

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
            if self._ws is None:
                continue
            await self._ws.send_json({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": f"（お知らせ）{text}"}]},
            })
            await self._ws.send_json({"type": "response.create"})

    # 受ける

    async def _receive(self, ws, on_said: Callable[[str, str], None] | None) -> None:
        said: list[str] = []
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(message.data)
            except ValueError:
                continue
            kind = event.get("type", "")
            if kind == "response.output_audio.delta":
                self.speaking_item = str(event.get("item_id") or self.speaking_item)
                self.speaker.write(base64.b64decode(event.get("delta") or ""))
            elif kind == "input_audio_buffer.speech_started":
                await self._interrupt(ws)
            elif kind == "response.output_audio_transcript.done":
                text = str(event.get("transcript") or "").strip()
                if text and on_said:
                    on_said("Kei", text)
                said.append(text)
            elif kind == "conversation.item.input_audio_transcription.completed":
                text = str(event.get("transcript") or "").strip()
                if text and on_said:
                    on_said("依頼者", text)
            elif kind == "response.done":
                await self._answer_tools(ws, event)
            elif kind == "error":
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

    async def _answer_tools(self, ws, event: dict) -> None:
        """道具を呼ばれていたら、答えを返して続きを喋らせる。"""
        outputs = ((event.get("response") or {}).get("output") or [])
        calls = [o for o in outputs if isinstance(o, dict) and o.get("type") == "function_call"]
        if not calls:
            return
        loop = asyncio.get_running_loop()
        for call in calls:
            name = str(call.get("name") or "")
            try:
                arguments = json.loads(call.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            log.info("道具を呼ばれました: %s %s", name, json.dumps(arguments, ensure_ascii=False)[:120])
            # 道具の中で Codex を動かすことがあるので、別のスレッドに出す
            answer = await loop.run_in_executor(None, self.tools.call, name, arguments)
            await ws.send_json({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output",
                         "call_id": call.get("call_id"),
                         "output": json.dumps({"text": answer}, ensure_ascii=False)},
            })
        # これを送らないとモデルは黙ったまま
        await ws.send_json({"type": "response.create"})

    def close(self) -> None:
        self.speaker.close()
