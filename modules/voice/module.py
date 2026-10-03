"""声のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 出来事（on_event）… 本体やほかのモジュールが配った出来事を、声の担当プロセスに投げっぱなしで渡す。
  言い方と顔は担当が決める。通知設定の「知らせる」を切っていれば渡さない
- MCP（head_action の voice）… 「知らせる」「聞く（Mac のマイク）」の設定。どちらも既定は切で、再起動しても戻る。
  「聞く」を変えたら、担当にマイクを開ける・閉じるを伝える（知らせを切っていても、聞くのは止められる。逆も同じ）
  通常の音声通話は Dot を使う。このモジュールは Mac や机のロボットで話したいときの選択肢
"""

from __future__ import annotations

import asyncio

from kei_agent.api import Core

from .skills import LISTEN_KEY, NOTIFY, NOTIFY_KEY, SWITCH


class Module:
    def __init__(self, core: Core):
        self.core = core
        self._switch_lock = asyncio.Lock()

    def is_on(self, key: str) -> bool:
        """通知設定が有効か（既定は切。不正な保存値でも急に喋り出さない）。"""
        return (self.core.records.get(SWITCH, key) or {}).get("on") is True

    async def on_event(self, kind: str, data: dict) -> None:
        if self.is_on(NOTIFY_KEY):
            await self.core.tell_agent(NOTIFY, {"kind": kind, **data})

    async def head_action(self, name: str, params: dict) -> dict | None:
        """MCP の voice からの切り替え。None を渡したほうは変えない。今の2つの設定を返す。"""
        if name != "voice":
            return None
        notify, listen = params.get("notify"), params.get("listen")
        for key, value in ((NOTIFY_KEY, notify), (LISTEN_KEY, listen)):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{key} は true / false を指定してください")
        async with self._switch_lock:
            await self._switch(self.is_on(NOTIFY_KEY) if notify is None else notify,
                               self.is_on(LISTEN_KEY) if listen is None else listen)
            return {"notify": self.is_on(NOTIFY_KEY), "listen": self.is_on(LISTEN_KEY)}

    async def _switch(self, notify: bool, listen: bool) -> None:
        if listen != self.is_on(LISTEN_KEY):
            # 常に録らない。入れたときだけ開ける（担当はマイクを開けていないあいだ、繋がりごと切っている）
            try:
                received = await self.core.tell_agent(NOTIFY, {"kind": "listen", "on": listen})
            except Exception as e:
                raise ValueError("マイクの設定を声の担当に渡せませんでした。担当を起動してからやり直してください") from e
            if not received:
                raise ValueError("マイクの設定を声の担当に渡せませんでした。担当を起動してからやり直してください")
            self.core.records.put(SWITCH, LISTEN_KEY, {"on": listen})
        self.core.records.put(SWITCH, NOTIFY_KEY, {"on": notify})
