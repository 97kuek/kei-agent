"""ほかのエージェントに仕事を頼む口（A2A のクライアント）。

Kei Agent 本体はオーケストレーターなので、A2A の呼ぶ側だけを持つ。相手の名刺を読み、
JSON-RPC で `SendMessage` し、終わるまで `GetTask` で見に行く（docs/architecture.md）。

依存を増やさないよう、SDK は使わずに aiohttp で薄く書いている。
仕様: https://a2a-protocol.org/latest/specification/
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp
from aiohttp.http_exceptions import LineTooLong

log = logging.getLogger(__name__)

CARD_PATH = "/.well-known/agent-card.json"
# JSON-RPC のメソッド名。A2A v1 は proto の RPC 名（SendMessage）、v0.x は message/send だった
SEND_MESSAGE = "SendMessage"
# 長い仕事は、経過を流しながら返してもらう（SSE）。途中の状態が data: 行で1つずつ届く
SEND_STREAMING_MESSAGE = "SendStreamingMessage"
GET_TASK = "GetTask"
# どの版で話すか。付けないと、相手は 0.3 で話しかけられたと解釈する
PROTOCOL_VERSION = "1.0"
VERSION_HEADER = "A2A-Version"
# 終わるまで見に行く間隔と、諦めるまでの時間
POLL_SECONDS = 2.0
DEFAULT_TIMEOUT = 300.0
# A2A のタスクの状態のうち、これ以上変わらないもの
DONE_STATES = {"completed", "failed", "canceled", "rejected",
               "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELLED", "TASK_STATE_REJECTED"}
FAILED_STATES = {"failed", "canceled", "rejected",
                 "TASK_STATE_FAILED", "TASK_STATE_CANCELLED", "TASK_STATE_REJECTED"}


class A2AError(RuntimeError):
    pass


class NotReachable(A2AError):
    """相手に話しかけられなかった（落ちている・入れ替えの最中・住所が違う）。

    エージェントを入れ直すと数秒つながらないので、呼ぶ側がこれだけを見て
    待ってやり直せるように、ほかの失敗と分けておく。
    """


# 相手が落ちている・住所が違う・途中で切れた、はすべて A2AError にして返す
# （呼ぶ側が aiohttp を知らずに済むように）
# LineTooLong（上限を超える1行）は ClientError ではないので、別に並べる
NETWORK_ERRORS = (aiohttp.ClientError, LineTooLong, OSError, TimeoutError, asyncio.TimeoutError)


@dataclass
class TaskResult:
    """頼んだ仕事の結果。"""
    state: str
    text: str
    task_id: str = ""
    # 最後の状態に付いていた文だけ（経過を流す仕事では、こちらが本当の返事）
    status_text: str = ""

    @property
    def ok(self) -> bool:
        return self.state not in FAILED_STATES

    @property
    def answer(self) -> str:
        """途中の経過を除いた返事。"""
        return self.status_text or self.text


def _parts_text(message: dict) -> str:
    parts = (message or {}).get("parts") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")).strip()


def _status_of(result: dict) -> tuple[str, str]:
    """イベントや結果から（状態, 付いている文）を取り出す。

    SendMessage は task を包んで返し、流しながら返すときは statusUpdate が届く。
    どちらの形でも同じように読めるようにしている。
    """
    for key in (None, "task", "statusUpdate", "status_update"):
        node = result if key is None else result.get(key)
        if isinstance(node, dict) and isinstance(node.get("status"), dict):
            status = node["status"]
            return str(status.get("state") or ""), _parts_text(status.get("message") or {})
    return "", _parts_text(result.get("message") or {})


def _status_text(result: dict) -> str:
    """いまの状態に付いている文（経過や、終わったときの返事）。"""
    return _status_of(result)[1]


def _task_id(result: dict) -> str:
    for key in (None, "task", "statusUpdate", "status_update"):
        node = result if key is None else result.get(key)
        if isinstance(node, dict):
            found = node.get("id") or node.get("taskId") or node.get("task_id")
            if isinstance(found, str) and found:
                return found
    return ""


def _texts(obj) -> list[str]:
    """返ってきた JSON から、text の Part を拾って並べる（形が版で変わっても拾えるように）。"""
    found = []
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str) and obj.get("text"):
            found.append(obj["text"])
        for value in obj.values():
            found += _texts(value)
    elif isinstance(obj, list):
        for value in obj:
            found += _texts(value)
    return found


class Agent:
    """1つのエージェントへの窓口。"""

    def __init__(self, base_url: str, token: str = "", timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._rpc_url: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {VERSION_HEADER: PROTOCOL_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def card(self, session: aiohttp.ClientSession | None = None) -> dict:
        """相手の名刺（何ができるか、どこに話しかけるか）。"""
        try:
            async with (_session(session) as http,
                        http.get(self.base_url + CARD_PATH, headers=self._headers()) as resp):
                if resp.status != 200:
                    raise A2AError(f"名刺を読めません（HTTP {resp.status}）: {self.base_url}")
                text = await resp.text()
        except NETWORK_ERRORS as e:
            raise NotReachable(f"つながりません（{self.base_url}）: {type(e).__name__}: {e}") from None
        try:
            return json.loads(text)
        except ValueError:
            # 別のものが同じポートで待っていると、JSON でない本文が返る
            raise A2AError(f"名刺が JSON ではありません（{self.base_url}）: {text[:200]}") from None

    async def rpc_url(self, session: aiohttp.ClientSession | None = None) -> str:
        """JSON-RPC の窓口。名刺に書かれた supported_interfaces から選ぶ。"""
        if self._rpc_url:
            return self._rpc_url
        card = await self.card(session)
        for interface in card.get("supportedInterfaces") or card.get("supported_interfaces") or []:
            if str(interface.get("protocolBinding", interface.get("protocol_binding", ""))).upper() == "JSONRPC":
                self._rpc_url = interface["url"]
                return self._rpc_url
        raise A2AError(f"JSON-RPC の窓口が名刺にありません: {self.base_url}")

    async def _call(self, http: aiohttp.ClientSession, method: str, params: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        try:
            async with http.post(await self.rpc_url(http), json=body, headers=self._headers()) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise A2AError(f"{method} が断られました（HTTP {resp.status}）: {text[:200]}")
        except NETWORK_ERRORS as e:
            # つなぐ前に断られたなら、まだ何も届いていないのでやり直してよい
            broken = NotReachable if isinstance(e, aiohttp.ClientConnectionError) else A2AError
            raise broken(f"{method} を送れません（{self.base_url}）: {type(e).__name__}: {e}") from None
        try:
            payload = json.loads(text)
        except ValueError:
            raise A2AError(f"{method} の返事が JSON ではありません（{self.base_url}）: {text[:200]}") from None
        if "error" in payload:
            raise A2AError(f"{method} でエラー: {payload['error']}")
        return payload.get("result") or {}

    async def ask(self, skill: str, text: str = "", params: dict | None = None,
                  session: aiohttp.ClientSession | None = None,
                  on_progress: Callable[[str], Awaitable[None]] | None = None,
                  poll_seconds: float = POLL_SECONDS) -> TaskResult:
        """仕事を頼んで、終わるまで待つ。

        `params` は相手への細かい指定（days など）。`on_progress` を渡すと、相手が流してくる
        途中の経過を、新しくなるたびに渡す（長い作業のあいだ、様子を見せるために使う）。
        """
        message = {"role": "ROLE_USER", "parts": [{"text": text or skill}], "messageId": uuid.uuid4().hex}
        async with _session(session) as http:
            result = await self._call(http, SEND_MESSAGE, {
                "message": message, "metadata": {"skill": skill} | (params or {})})
            task_id = _task_id(result)
            state = _state(result)
            seen = ""
            deadline = asyncio.get_running_loop().time() + self.timeout
            while task_id and state not in DONE_STATES:
                if asyncio.get_running_loop().time() > deadline:
                    raise A2AError(f"{skill} が {self.timeout:.0f} 秒で終わりませんでした")
                await asyncio.sleep(poll_seconds)
                result = await self._call(http, GET_TASK, {"id": task_id})
                state = _state(result)
                current = _status_text(result)
                if on_progress and current and current != seen and state not in DONE_STATES:
                    seen = current
                    await on_progress(current)
            return TaskResult(state=state, text="\n".join(_texts(result)).strip(), task_id=task_id,
                              status_text=_status_text(result))


    async def stream(self, skill: str, text: str = "", params: dict | None = None,
                     on_progress: Callable[[str], Awaitable[None]] | None = None,
                     session: aiohttp.ClientSession | None = None) -> TaskResult:
        """流しながら返してもらう（長い仕事向け）。経過が変わるたびに on_progress を呼ぶ。

        相手が終わるまで1本の接続を開けたままにするので、途中で黙る時間が長くても切れない。
        """
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": SEND_STREAMING_MESSAGE, "params": {
            "message": {"role": "ROLE_USER", "parts": [{"text": text or skill}], "messageId": uuid.uuid4().hex},
            "metadata": {"skill": skill} | (params or {})}}
        headers = self._headers() | {"Accept": "text/event-stream"}
        timeout = aiohttp.ClientTimeout(total=self.timeout, sock_read=self.timeout)
        task_id = state = answer = seen = ""
        try:
            async with (_session(session) as http,
                        http.post(await self.rpc_url(http), json=body, headers=headers, timeout=timeout) as resp):
                if resp.status != 200:
                    raise A2AError(f"{skill} が断られました（HTTP {resp.status}）: {(await resp.text())[:200]}")
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[len("data:"):].strip() or "{}")
                    except ValueError:
                        # 経過の1行が壊れていても仕事は続いている。終わりの行が来なければ下で断る
                        log.warning("%s の経過の行を読めません: %s", skill, line[:200])
                        continue
                    if "error" in payload:
                        raise A2AError(f"{skill} でエラー: {payload['error']}")
                    result = payload.get("result") or {}
                    task_id = _task_id(result) or task_id
                    now, message = _status_of(result)
                    state = now or state
                    if state in DONE_STATES:
                        answer = message or answer
                        break
                    if on_progress and message and message != seen:
                        seen = message
                        await on_progress(message)
        except NETWORK_ERRORS as e:
            # まだ何も届いていないうちにつながらなかったなら、_call と同じくやり直してよい。
            # 経過が届いたあとに切れたときは、相手が動いているかもしれないので二重に頼まない
            fresh = not (task_id or state or seen)
            broken = NotReachable if fresh and isinstance(e, aiohttp.ClientConnectionError) else A2AError
            raise broken(f"{skill} の途中でつながりが切れました（{self.base_url}）: "
                         f"{type(e).__name__}: {e}") from None
        if state not in DONE_STATES:
            raise A2AError(f"{skill} の返事が、終わる前に切れました（{state or '状態不明'}）")
        return TaskResult(state=state, text=answer, task_id=task_id, status_text=answer)


def _state(result: dict) -> str:
    state, _ = _status_of(result)
    return state or str(result.get("state") or "")


def _session(session: aiohttp.ClientSession | None):
    """渡されていれば使い回し、なければその場で作って閉じる。"""
    if session is not None:
        return _Borrowed(session)
    return aiohttp.ClientSession()


class _Borrowed:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def __aenter__(self) -> aiohttp.ClientSession:
        return self.session

    async def __aexit__(self, *exc) -> None:
        return None
