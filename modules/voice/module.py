"""声のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

- 出来事（on_event）… 本体やほかのモジュールが配った出来事を、声の担当プロセスに投げっぱなしで渡す。
  言い方と顔は担当が決める。App Home の「知らせる」を切っていれば渡さない
- App Home（home / on_home_action）… 「知らせる」「聞く（マイク）」のチェック。どちらも既定は切で、再起動しても戻る。
  「聞く」を変えたら、担当にマイクを開ける・閉じるを伝える（知らせを切っていても、聞くのは止められる。逆も同じ）
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
        self.core.records.put(SWITCH, NOTIFY_KEY, {"on": NOTIFY_KEY in chosen})
        listen = LISTEN_KEY in chosen
        if listen != self.is_on(LISTEN_KEY):
            self.core.records.put(SWITCH, LISTEN_KEY, {"on": listen})
            # 常に録らない。入れたときだけ開ける（担当はマイクを開けていないあいだ、繋がりごと切っている）
            await self.core.tell_agent(NOTIFY, {"kind": "listen", "on": listen})
