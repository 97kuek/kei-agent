"""長くなったスレッドを区切って、新しいスレッドで続ける。

1つのスレッドにデータの準備から実験の検討までが積み重なると、読み返せなくなり、Claude の会話も重くなる。
作業がひと段落したとき（Claude が返答の最後に `🧵 区切り:` と書いたとき）か、依頼の数が
config.toml の handoff_after_turns に達したときに、返事の下に [新しいスレッドで続ける] ボタンを出す。

押すと、今までの会話の Claude に引き継ぎメモを書かせ、それをチャンネルに新しいスレッドとして投稿する。
新しいスレッドの最初の回には、そのメモを渡して新しい会話を始める（前の会話は持ち越さない）。

Assistant に混ぜて使う。self.slack、self.store、self.config などは Assistant のもの。
"""

from __future__ import annotations

import logging

from kei_agent import themes
from kei_agent.auto_messages import HANDOFF_MEMO_PROMPT, handoff_start_prompt
from kei_agent.request import Request
from kei_agent.slack_text import FAILED_PREFIX, HANDOFF_MARKER
from kei_agent.theme_files import append_thread_log, thread_log_path
from kei_agent.themes import ChannelKind, Workspace

log = logging.getLogger(__name__)

ACCEPT_ACTION = "kei_agent_handoff_accept"
DECLINE_ACTION = "kei_agent_handoff_decline"
# 区切りを勧めるチャンネル。#00_kei-agent（自分を直す話）は1スレッド1件なので勧めない
HANDOFF_KINDS = (ChannelKind.THEME, ChannelKind.OVERVIEW)
TITLE_LIMIT = 60


def handoff_title(text: str) -> str | None:
    """返答の `🧵 区切り: 題` の題。合図がなければ None、題が空なら ""。"""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith(HANDOFF_MARKER):
            return line[len(HANDOFF_MARKER):].strip()[:TITLE_LIMIT]
    return None


def strip_handoff(text: str) -> str:
    """合図の行は、ボタンに題を出すので本文からは消す。"""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith(HANDOFF_MARKER)).rstrip()


def split_memo(memo: str) -> tuple[str, str]:
    """引き継ぎメモを、1行目の題と残りに分ける。"""
    lines = memo.strip().splitlines()
    if not lines:
        return "続き", ""
    title = lines[0].strip().strip("#*").strip()[:TITLE_LIMIT] or "続き"
    return title, "\n".join(lines[1:]).strip()


class Handoff:
    def should_offer_handoff(self, req: Request, ws: Workspace, text: str, busy: bool) -> tuple[bool, str]:
        """ボタンを出すか、出すなら題（分からなければ ""）。busy は返事待ちやジョブが走っているとき。"""
        if ws.kind not in HANDOFF_KINDS or req.trigger == "handoff" or busy:
            return False, ""
        row = self.store.get_thread(req.channel, req.thread_ts)
        if row is None or row["handed_off_to"]:
            return False, ""
        title = handoff_title(text)
        if title is not None:
            return True, title
        every = self.config.handoff_after_turns
        return bool(every) and row["turns"] - row["handoff_offered_at"] >= every, ""

    async def offer_handoff(self, req: Request, title: str) -> None:
        row = self.store.get_thread(req.channel, req.thread_ts)
        self.store.update_thread(req.channel, req.thread_ts, handoff_offered_at=row["turns"] if row else 0)
        text = "🧵 ここで区切って、新しいスレッドで続けると読みやすいと思う"
        if title:
            text += f"\n次のスレッド: *{title}*"
        await self.slack.chat_postMessage(
            channel=req.channel, thread_ts=req.thread_ts, text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text":
                    "押すと、ここまでの要点と次にやることをまとめて、チャンネルに新しいスレッドを立てるよ"}]},
                {"type": "actions", "elements": [
                    {"type": "button", "action_id": ACCEPT_ACTION, "style": "primary",
                     "text": {"type": "plain_text", "text": "新しいスレッドで続ける"}, "value": req.thread_ts},
                    {"type": "button", "action_id": DECLINE_ACTION,
                     "text": {"type": "plain_text", "text": "このまま続ける"}, "value": req.thread_ts},
                ]},
            ],
        )

    async def on_handoff_action(self, body: dict) -> None:
        """[新しいスレッドで続ける] [このまま続ける] が押された。押せるのは依頼者だけ。"""
        if not self.is_allowed(body.get("user", {}).get("id")):
            return
        action = (body.get("actions") or [{}])[0]
        channel = body.get("container", {}).get("channel_id") or body.get("channel", {}).get("id")
        thread_ts = action.get("value", "")
        row = self.store.get_thread(channel, thread_ts) if channel else None
        if row is None or row["handed_off_to"]:
            return
        if action.get("action_id") != ACCEPT_ACTION:
            await self.replace_buttons(body, "このまま続けるね", channel)
            return
        await self.replace_buttons(body, "🧵 新しいスレッドに引き継いでいるよ…", channel)
        req = Request(channel, row["channel_name"], thread_ts, None, HANDOFF_MEMO_PROMPT, trigger="handoff")
        self.spawn(self.hand_off(req))

    async def hand_off(self, req: Request) -> str | None:
        """引き継ぎメモを書かせて、新しいスレッドを立てる。立てたスレッドの ts を返す。"""
        ws = themes.resolve(self.config, req.channel_name)
        themes.ensure_workspace(ws)
        assert ws.cwd is not None
        async with self.thread_locks[(req.channel, req.thread_ts)], self.semaphore:
            row = self.store.get_thread(req.channel, req.thread_ts)
            if row is None or row["handed_off_to"]:
                return None  # 2度押しで、もう引き継いである
            run_id = self.store.start_run(req.channel, req.thread_ts, req.channel_name, req.trigger)
            # 途中で終了させられても、次の起動で拾えるように控えておく（resume_interrupted）
            in_flight = self.store.start_in_flight(req.to_payload())
            try:
                result = await self._converse(req, ws, req.text, None)
            except BaseException:
                # 控えは残したまま（次の起動でやり直す）、走りっぱなしの記録だけ閉じる
                self.store.end_run(run_id, is_error=True, cost_usd=None)
                raise
            self.store.end_run(run_id, result.is_error, result.cost_usd)
            self.store.finish_deferred(in_flight)
            memo, contract_failed = self.render_reply(result)
            if result.is_error or contract_failed:
                await self.post(req, f"{FAILED_PREFIX} 引き継ぎのまとめを作れなかったよ。"
                                     "もう一度区切りたいときは「新しいスレッドにして」と書いてね。")
                return None
            title, body = split_memo(memo)
            old_link = await self.permalink(req.channel, req.thread_ts)
            resp = await self.slack.chat_postMessage(channel=req.channel, markdown_text=(
                f"🧵 **{title}**\n\n{body}\n\n[前のスレッド]({old_link})"
                "　続きはこのスレッドに返信してね（メンションなしでいいよ）"))
            new_ts = resp["ts"]
            previous_log = thread_log_path(ws.cwd, req.thread_ts).relative_to(ws.cwd)
            self.store.upsert_thread(req.channel, new_ts, req.channel_name, None)
            self.store.update_thread(req.channel, new_ts, handoff_memo=handoff_start_prompt(memo, str(previous_log)))
            self.store.update_thread(req.channel, req.thread_ts, handed_off_to=new_ts)
            self.store.set_awaiting(req.channel, req.thread_ts, False)
            append_thread_log(ws.cwd, req.channel_name, new_ts, "Kei Agent（引き継ぎ）", memo)
        new_link = await self.permalink(req.channel, new_ts)
        await self.post(req, f"🧵 ここから先は新しいスレッドで続けるね: <{new_link}|{title}>")
        return new_ts

    def handoff_memo_for(self, row) -> str:
        """新しいスレッドの最初の回（まだ会話がないとき）に、依頼の前に付ける引き継ぎメモ。"""
        if row is None or row["session_id"]:
            return ""
        return row["handoff_memo"] or ""
