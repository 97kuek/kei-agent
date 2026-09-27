"""研究の担当プロセス（A2A のサーバー）。起動は共通のコマンド `kei-agent-module research`。

できるのは2つ。テーマの作業場で provider を1回動かすこと（どの担当とも同じ `ask`）と、長い処理（pueue のジョブ）の
出し入れ。依頼は JSON で届く。

    ask         {"channel_name": "amr-query", "prompt": "図を作って", "session_id": null,
                 "channel": "C1", "thread_ts": "1.2", "allowed_domains": ["example.com"]}
    submit-job  {"cwd": "~/research/amr-query", "command": "uv run x.py", "label": "kei-agent-3"}
    list-jobs   {}
    cancel-job / forget-job  {"task_id": 12}

ジョブが「どのスレッドのものか」「できるはずのファイルは何か」は、本体（kei_agent.jobs）が覚えている。
ここは pueue の待ち行列を持つだけ。会話の続け方（session の付け替え、履歴の戻し）と Slack への見せ方も本体の仕事。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kei_agent_a2a.api import (
    ASK,
    CANCEL_JOB,
    FORGET_JOB,
    LIST_JOBS,
    SUBMIT_JOB,
    AgentSkill,
    Config,
    Pueue,
    SkillExecutor,
    TaskUpdater,
    Workspace,
    channel_workspace,
    theme_folders,
)

log = logging.getLogger(__name__)

DESCRIPTION = "研究テーマの作業場で、選択済み provider を1回動かす。長い処理は pueue のジョブにする"
SKILLS = [
    AgentSkill(
        id=ASK,
        name="研究用 provider を1回動かす",
        description="JSON（channel_name・prompt・session_id・allowed_domains）を受け取り、テーマの作業場で"
                    "選択済み provider を1回動かして、答えを返す",
        tags=["research"],
        examples=['{"channel_name": "amr-query", "prompt": "図を作って"}'],
    ),
    AgentSkill(
        id=SUBMIT_JOB,
        name="ジョブを投入する",
        description="JSON（cwd・command・label）を受け取り、pueue の待ち行列に入れて task_id を返す。"
                    "cwd は研究テーマの中か、研究全体の作業場だけ",
        tags=["research", "jobs"],
        examples=['{"cwd": "~/research/amr-query", "command": "uv run train.py", "label": "kei-agent-3"}'],
    ),
    AgentSkill(id=LIST_JOBS, name="ジョブの状態",
               description="待ち行列にあるジョブの状態をまとめて返す（data.tasks に pueue の中身）",
               tags=["research", "jobs"], examples=[]),
    AgentSkill(id=CANCEL_JOB, name="ジョブを止める", description="JSON（task_id）で、走っているジョブを止める",
               tags=["research", "jobs"], examples=[]),
    AgentSkill(id=FORGET_JOB, name="ジョブを片づける", description="JSON（task_id）で、終わったジョブを待ち行列から消す",
               tags=["research", "jobs"], examples=[]),
]
NAMES = tuple(skill.id for skill in SKILLS)
NO_JSON = "依頼は JSON で渡してください"


def _json(text: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        raise ValueError(NO_JSON) from None
    if not isinstance(data, dict):
        raise ValueError(NO_JSON)
    return data


class Executor(SkillExecutor):
    # 制限の表とモデルの一覧を引く名前（共通の起動コマンドも同じ名前を入れる）
    agent = "research"

    def __init__(self, config: Config | None = None, store=None, pueue: Pueue | None = None):
        super().__init__(config, store)
        self.pueue = pueue or Pueue(self.config)
        # pueue のグループは最初に使うときだけ用意する
        self._group_ready = False

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = metadata.get("skill", ASK)
        if skill not in NAMES:
            await self.fail(updater, f"できるのは {' / '.join(NAMES)} です")
            return
        if skill == ASK:
            await self.answer(updater, text)
            return
        try:
            ask = _json(text)
        except ValueError as e:
            await self.fail(updater, str(e))
            return
        await self._job(updater, skill, ask)

    def workspace(self, ask: dict) -> Workspace:
        """研究はテーマの作業場で動かす。許可済みの接続先は、本体が依頼に添えてくる。"""
        return channel_workspace(self.config, str(ask.get("channel_name") or ""), ask.get("allowed_domains") or (),
                                 create=not ask.get("read_only"))

    async def _job(self, updater: TaskUpdater, skill: str, ask: dict) -> None:
        """長い処理（pueue のジョブ）。どのスレッドのジョブかはオーケストレーターが覚えている。"""
        try:
            if skill == SUBMIT_JOB:
                cwd = self._job_dir(str(ask.get("cwd") or ""))
                if not self._group_ready:
                    await self.pueue.ensure_group()
                    self._group_ready = True
                task_id = await self.pueue.add(cwd, str(ask.get("command") or ""),
                                               label=str(ask.get("label") or ""))
                await self.done(updater, f"ジョブを入れました（pueue {task_id}）", {"task_id": task_id})
            elif skill == LIST_JOBS:
                tasks = await self.pueue.tasks()
                await self.done(updater, f"動いているジョブ: {len(tasks)} 件",
                                 {"tasks": {str(k): v for k, v in tasks.items()}})
            elif skill == CANCEL_JOB:
                await self.pueue.kill(int(ask["task_id"]))
                await self.done(updater, f"ジョブを止めました（pueue {ask['task_id']}）")
            else:
                await self.pueue.remove(int(ask["task_id"]))
                await self.done(updater, f"ジョブを片づけました（pueue {ask['task_id']}）")
        except (KeyError, TypeError, ValueError) as e:
            await self.fail(updater, f"ジョブの依頼が読めません: {e}")
        except RuntimeError as e:
            await self.fail(updater, f"pueue が失敗しました: {e}")

    def _job_dir(self, cwd: str) -> Path:
        """ジョブを動かしてよい場所だけを受け付ける（渡された場所で何でも動かさない）。

        研究テーマの中（既存のフォルダを使うテーマも）と、研究全体の作業場（`<agent_root>/overview`）。
        研究テーマを並べた場所（`research_root`）そのものでは動かさない。
        """
        overview = self.config.overview_dir.resolve()
        path = Path(cwd).expanduser().resolve()
        folders = [folder.resolve() for folder in theme_folders(self.config).values()]
        inside = any(path == folder or folder in path.parents for folder in folders) \
            or overview == path or overview in path.parents
        if not path.is_dir() or not inside:
            raise ValueError(f"ジョブを動かしてよい場所ではありません: {cwd}")
        return path
