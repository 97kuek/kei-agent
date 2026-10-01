"""仕事の開発のモジュールの、本体（オーケストレーター）側の動き。コアには kei_agent.api の窓口でだけ触れる。

仕事のプロジェクトのチャンネル（module.toml の [channels] に "work-*"）を受け持ち、依頼はプロジェクトの作業場で
担当と会話して答える（core.work。添付・できたファイル・接続先の許可・引き継ぎは研究テーマと同じ流れ）。
"""

from __future__ import annotations

from kei_agent.api import Core, Request


class Module:
    def __init__(self, core: Core):
        self.core = core

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        await self.core.work(req)
