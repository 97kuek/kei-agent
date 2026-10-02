"""声のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 出来事（on_event）… 本体やほかのモジュールが配った出来事を、声の担当プロセスに投げっぱなしで渡す。
  言い方と顔は担当が決める。App Home の「知らせる」を切っていれば渡さない
- App Home（home / on_home_action）… 「知らせる」「聞く（マイク）」のチェック。どちらも既定は切で、再起動しても戻る。
  「聞く」を変えたら、担当にマイクを開ける・閉じるを伝える（知らせを切っていても、聞くのは止められる。逆も同じ）
- 頭から（head_action の voice）… 同じ2つのスイッチ。Slack につながず App Home が無いときは、こちらで変える
"""

from __future__ import annotations

from kei_agent.api import Core, selected_values

from .skills import LISTEN_KEY, NOTIFY, NOTIFY_KEY, SWITCH

# App Home のチェックの名前と、並べる項目
SWITCHES = "switches"
OPTIONS = {NOTIFY_KEY: "知らせる", LISTEN_KEY: "聞く（マイク）"}


class Module:
    def __init__(self, core: Core):
        self.core = core

    def is_on(self, key: str) -> bool:
        """App Home のチェックが入っているか（既定は切。机にロボットが無いのに急に喋り出さない）。"""
        return bool((self.core.records.get(SWITCH, key) or {}).get("on"))

    async def on_event(self, kind: str, data: dict) -> None:
        if self.is_on(NOTIFY_KEY):
            await self.core.tell_agent(NOTIFY, {"kind": kind, **data})

    def home(self) -> list[dict]:
        chosen = {key for key in OPTIONS if self.is_on(key)}
        return [{"type": "actions", "elements": [self.core.home_checkboxes(SWITCHES, OPTIONS, chosen)]}]

    async def on_home_action(self, name: str, action: dict) -> None:
        if name != SWITCHES:
            return
        chosen = selected_values(action)
        await self._switch(NOTIFY_KEY in chosen, LISTEN_KEY in chosen)

    async def head_action(self, name: str, params: dict) -> dict | None:
        """頭（手の口の voice）からの切り替え。None を渡したほうは変えない。今の2つのスイッチを返す。"""
        if name != "voice":
            return None
        notify, listen = params.get("notify"), params.get("listen")
        await self._switch(self.is_on(NOTIFY_KEY) if notify is None else bool(notify),
                           self.is_on(LISTEN_KEY) if listen is None else bool(listen))
        return {"notify": self.is_on(NOTIFY_KEY), "listen": self.is_on(LISTEN_KEY)}

    async def _switch(self, notify: bool, listen: bool) -> None:
        self.core.records.put(SWITCH, NOTIFY_KEY, {"on": notify})
        if listen != self.is_on(LISTEN_KEY):
            self.core.records.put(SWITCH, LISTEN_KEY, {"on": listen})
            # 常に録らない。入れたときだけ開ける（担当はマイクを開けていないあいだ、繋がりごと切っている）
            await self.core.tell_agent(NOTIFY, {"kind": "listen", "on": listen})
