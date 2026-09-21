"""声のレイヤに、出来事を知らせる（オーケストレーター側）。

渡すのは「何が起きたか」だけ。言い方も顔も声のレイヤが決める（docs/voice.md の4節）。
本体に「どんな顔をさせるか」を持たせると、対応表が2か所に散る。

**返事を待たない。** 待つと、Slack の処理が机の上のロボットの再生時間に引きずられる。
送れなかったことは、ログに残す以上のことをしない（声が出ないだけで、Slack の仕事は終わっている）。

Assistant に混ぜて使う。self.agents、self.store などは Assistant のもの。
"""

from __future__ import annotations

import json
import logging

from kei_agent import agents, settings

log = logging.getLogger(__name__)

# config.toml の [a2a.agents] で書くエージェントの名前
AGENT = "voice"
NOTIFY = "notify"


class VoiceNotices:
    def notify_voice(self, kind: str, **fields) -> None:
        """出来事を声のレイヤに投げる。届かなくても、呼んだ側は何も気にしなくてよい。

        住所が書かれていない（`[a2a.agents]` に `voice` が無い）ときと、
        App Home で切っているときは、何もしない。
        """
        agent = self.agents.get(AGENT)
        if agent is None or not settings.voice_enabled(self.store):
            return
        event = {"kind": kind, **{k: v for k, v in fields.items() if v not in (None, "")}}
        self.spawn(self._notify(agent, event))

    def notify_listening(self, on: bool) -> None:
        """マイクを開ける・閉じるを伝える。

        「聞く」は「知らせる」とは別に送る。知らせを切っていても、聞くのは止められるようにする
        （逆も同じ）。
        """
        agent = self.agents.get(AGENT)
        if agent is not None:
            self.spawn(self._notify(agent, {"kind": "listen", "on": on}))

    async def _notify(self, agent, event: dict) -> None:
        reply = await agents.ask(agent, NOTIFY, text=json.dumps(event, ensure_ascii=False))
        if not reply.ok:
            # 知らせるだけのことなので、依頼者に見せるほどではない
            log.info("声のレイヤに知らせられません: %s", reply.text[:200])
