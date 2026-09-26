"""頼まれた作業（provider を1回動かす／長い処理をジョブにする）をこなすところ。

できるのは2つ。provider を1回動かすこと（どのエージェントでも同じ `ask`）と、長い処理（pueue のジョブ）の出し入れ。
依頼は JSON で届く。

    ask         {"channel_name": "amr-query", "prompt": "図を作って", "session_id": null,
                 "channel": "C1", "thread_ts": "1.2", "allowed_domains": ["example.com"]}
    submit-job  {"cwd": "~/research/amr-query", "command": "uv run x.py", "label": "kei-agent-3"}
    list-jobs   {}
    cancel-job / forget-job  {"task_id": 12}

ジョブが「どのスレッドのものか」「できるはずのファイルは何か」は、オーケストレーターが覚えている。
ここは pueue の待ち行列を持つだけ（docs/architecture.md）。

`ask` の依頼と返事の形、経過と柵の扱いは `kei_agent_a2a`（大学・仕事のエージェントと共通）。
研究だけが違うのは、動かす場所がテーマの作業場になること。

会話の続け方（session の付け替え、履歴の戻し）と Slack への見せ方は持たない。
それはオーケストレーターの仕事（docs/architecture.md）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from a2a.server.tasks import TaskUpdater

from kei_agent import themes
from kei_agent.config import Config, load_config
from kei_agent.jobs import Pueue
from kei_agent.store import Store
from kei_agent.themes import Workspace
from kei_agent_a2a.executor import ASK, SkillExecutor
from kei_agent_research.skills import CANCEL_JOB, FORGET_JOB, LIST_JOBS, SUBMIT_JOB

log = logging.getLogger(__name__)

SKILLS = (ASK, SUBMIT_JOB, LIST_JOBS, CANCEL_JOB, FORGET_JOB)
NO_JSON = "依頼は JSON で渡してください"


def _json(text: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        raise ValueError(NO_JSON) from None
    if not isinstance(data, dict):
        raise ValueError(NO_JSON)
    return data


class ResearchExecutor(SkillExecutor):
    agent = "research"

    def __init__(self, config: Config | None = None, pueue: Pueue | None = None, store: Store | None = None):
        self.config = config or load_config()
        self.pueue = pueue or Pueue(self.config)
        self.store = store or Store(self.config.db_path)
        # pueue のグループは最初に使うときだけ用意する
        self._group_ready = False

    async def handle(self, updater: TaskUpdater, metadata: dict, text: str) -> None:
        skill = metadata.get("skill", ASK)
        if skill not in SKILLS:
            await self._fail(updater, f"できるのは {' / '.join(SKILLS)} です")
            return
        if skill == ASK:
            await self.answer(updater, text)
            return
        try:
            ask = _json(text)
        except ValueError as e:
            await self._fail(updater, str(e))
            return
        await self._job(updater, skill, ask)

    def workspace(self, ask: dict) -> Workspace:
        """研究はテーマの作業場で動かす。許可済みの接続先は、本体が依頼に添えてくる。"""
        ws = themes.resolve(self.config, str(ask.get("channel_name") or ""))
        if ws.cwd is None:
            raise ValueError(f"#{ws.channel_name} には作業用ディレクトリがありません")
        ws = replace(ws, allowed_domains=tuple(ask.get("allowed_domains") or ()))
        if not ask.get("read_only"):
            themes.ensure_workspace(ws)
        return ws

    async def _job(self, updater: TaskUpdater, skill: str, ask: dict) -> None:
        """長い処理（pueue のジョブ）。どのスレッドのジョブかはオーケストレーターが覚えている。"""
        try:
            if skill == SUBMIT_JOB:
                cwd = self._theme_dir(str(ask.get("cwd") or ""))
                if not self._group_ready:
                    await self.pueue.ensure_group()
                    self._group_ready = True
                task_id = await self.pueue.add(cwd, str(ask.get("command") or ""),
                                               label=str(ask.get("label") or ""))
                await self._done(updater, f"ジョブを入れました（pueue {task_id}）", {"task_id": task_id})
            elif skill == LIST_JOBS:
                tasks = await self.pueue.tasks()
                await self._done(updater, f"動いているジョブ: {len(tasks)} 件",
                                 {"tasks": {str(k): v for k, v in tasks.items()}})
            elif skill == CANCEL_JOB:
                await self.pueue.kill(int(ask["task_id"]))
                await self._done(updater, f"ジョブを止めました（pueue {ask['task_id']}）")
            else:
                await self.pueue.remove(int(ask["task_id"]))
                await self._done(updater, f"ジョブを片づけました（pueue {ask['task_id']}）")
        except (KeyError, TypeError, ValueError) as e:
            await self._fail(updater, f"ジョブの依頼が読めません: {e}")
        except RuntimeError as e:
            await self._fail(updater, f"pueue が失敗しました: {e}")

    def _theme_dir(self, cwd: str) -> Path:
        """ジョブを動かしてよい場所だけを受け付ける（渡された場所で何でも動かさない）。

        研究テーマの中と、研究全体の作業場（`<agent_root>/overview`。研究テーマの外にある）。
        研究テーマを並べた場所（`research_root`）そのものでは動かさない。
        """
        research_root = self.config.research_root.resolve()
        overview = self.config.overview_dir.resolve()
        path = Path(cwd).expanduser().resolve()
        # 研究テーマの親（research_root そのもの）は、どのテーマでもないので断る
        inside = research_root in path.parents or overview == path or overview in path.parents
        if not path.is_dir() or not inside:
            raise ValueError(f"ジョブを動かしてよい場所ではありません: {cwd}")
        return path
