"""ほかのエージェントに仕事を頼む口（A2A のクライアント）。

Kei Agent 本体はオーケストレーターなので、A2A の呼ぶ側だけを持つ。相手の名刺を読み、
JSON-RPC で `SendMessage` し、終わるまで `GetTask` で見に行く（docs/plan.md の16章）。

依存を増やさないよう、SDK は使わずに aiohttp で薄く書いている。
仕様: https://a2a-protocol.org/latest/specification/
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

CARD_PATH = "/.well-known/agent-card.json"
# JSON-RPC のメソッド名。A2A v1 は proto の RPC 名（SendMessage）、v0.x は message/send だった
SEND_MESSAGE = "SendMessage"
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


@dataclass
class TaskResult:
    """頼んだ仕事の結果。"""
    state: str
    text: str
    task_id: str = ""

    @property
    def ok(self) -> bool:
        return self.state not in FAILED_STATES


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
        async with (_session(session) as http,
                    http.get(self.base_url + CARD_PATH, headers=self._headers()) as resp):
            if resp.status != 200:
                raise A2AError(f"名刺を読めません（HTTP {resp.status}）: {self.base_url}")
            return json.loads(await resp.text())

    async def rpc_url(self, session: aiohttp.ClientSession | None = None) -> str:
        """JSON-RPC の窓口。名刺に書かれた supported_interfaces から選ぶ。"""
        if self._rpc_url:
            return self._rpc_url
        card = await self.card(session)
        for interface in card.get("supportedInterfaces") or card.get("supported_interfaces") or []:
            if str(interface.get("protocolBinding", interface.get("protocol_binding", ""))).upper() == "JSONRPC":
                self._rpc_url = interface["url"]
                return self._rpc_url
        # 旧い版の名刺（url だけを持つ）にも当てる
        if card.get("url"):
            self._rpc_url = card["url"]
            return self._rpc_url
        raise A2AError(f"JSON-RPC の窓口が名刺にありません: {self.base_url}")

    async def _call(self, http: aiohttp.ClientSession, method: str, params: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        async with http.post(await self.rpc_url(http), json=body, headers=self._headers()) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise A2AError(f"{method} が断られました（HTTP {resp.status}）: {text[:200]}")
        payload = json.loads(text)
        if "error" in payload:
            raise A2AError(f"{method} でエラー: {payload['error']}")
        return payload.get("result") or {}

    async def ask(self, skill: str, text: str = "", session: aiohttp.ClientSession | None = None) -> TaskResult:
        """仕事を頼んで、終わるまで待つ。"""
        message = {"role": "ROLE_USER", "parts": [{"text": text or skill}], "messageId": uuid.uuid4().hex}
        async with _session(session) as http:
            result = await self._call(http, SEND_MESSAGE, {
                "message": message, "metadata": {"skill": skill}})
            task_id = result.get("id") or (result.get("task") or {}).get("id") or ""
            state = _state(result)
            deadline = asyncio.get_running_loop().time() + self.timeout
            while task_id and state not in DONE_STATES:
                if asyncio.get_running_loop().time() > deadline:
                    raise A2AError(f"{skill} が {self.timeout:.0f} 秒で終わりませんでした")
                await asyncio.sleep(POLL_SECONDS)
                result = await self._call(http, GET_TASK, {"id": task_id})
                state = _state(result)
            return TaskResult(state=state, text="\n".join(_texts(result)).strip(), task_id=task_id)


def _state(result: dict) -> str:
    status = result.get("status")
    if isinstance(status, dict):
        return str(status.get("state") or "")
    return str(status or result.get("state") or "")


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
