"""仕事の開発の担当プロセス（A2A のサーバー）。起動は共通のコマンド `kei-agent-module workdev`。

できるのは、プロジェクトの作業場で provider を1回動かすこと（どの担当とも同じ `ask`）。会社のアカウントで動く
（agents.csv の workdev の行の claude_account・codex_account）。依頼は JSON で届く。

    ask  {"channel_name": "work-billing", "prompt": "テストを直して", "session_id": null,
          "channel": "C1", "thread_ts": "1.2", "allowed_domains": ["example.com"]}
"""

from __future__ import annotations

from kei_agent_a2a.api import ASK, AgentSkill, SkillExecutor, TaskUpdater, Workspace, channel_workspace

DESCRIPTION = "仕事のプロジェクトの作業場で、選択済み provider を1回動かす（コードを書き、コマンドとテストを動かす）"
SKILLS = [
    AgentSkill(
        id=ASK,
        name="プロジェクトで provider を1回動かす",
        description="JSON（channel_name・prompt・session_id・allowed_domains）を受け取り、プロジェクトの作業場で"
                    "選択済み provider を1回動かして、答えを返す",
        tags=["work", "code"],
        examples=['{"channel_name": "work-billing", "prompt": "テストを直して"}'],
    ),
]


class Executor(SkillExecutor):
    # 制限の表とモデルの一覧を引く名前（共通の起動コマンドも同じ名前を入れる）
    agent = "workdev"

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        if metadata.get("skill", ASK) != ASK:
            await self.fail(updater, f"できるのは {ASK} だけです")
            return
        await self.answer(updater, text)

    def workspace(self, ask: dict) -> Workspace:
        """プロジェクトの作業場で動かす。許可済みの接続先は、本体が依頼に添えてくる。"""
        return channel_workspace(self.config, str(ask.get("channel_name") or ""), ask.get("allowed_domains") or (),
                                 create=not ask.get("read_only"))
