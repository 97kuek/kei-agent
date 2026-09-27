"""研究のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

ほかのどれにも当たらないチャンネルを研究テーマとして受け持ち（module.toml の [channels] に "*"）、依頼はテーマの
作業場で研究の担当と会話して答える（core.work。添付・できたファイル・接続先の許可・引き継ぎ・ジョブも同じ流れ）。
"""

from __future__ import annotations

from kei_agent.api import Core, Request


class Module:
    def __init__(self, core: Core):
        self.core = core

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        await self.core.work(req)
