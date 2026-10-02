"""Slack のボタンの書き換えと、App Home（docs/architecture.md）。

Assistant に混ぜて使う。self.slack、self.store、self.config、self.submit などは Assistant のもの。
"""

from __future__ import annotations

import logging

from kei_agent.conversation import home

log = logging.getLogger(__name__)


class SettingsActions:
    async def replace_buttons(self, body: dict, text: str, fallback_channel: str = "") -> None:
        """押されたボタンのメッセージを、決まった内容の1行に書き換える（2度押しできないようにする）。"""
        container = body.get("container", {})
        try:
            await self.slack.chat_update(
                channel=container.get("channel_id") or fallback_channel, ts=container.get("message_ts"),
                text=text, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
        except Exception:
            log.warning("ボタンのメッセージを書き換えられません", exc_info=True)

    # App Home（設定画面）

    async def publish_home(self, user_id: str) -> None:
        owner = self.is_allowed(user_id)
        view = home.build_home(self.config, self.store, is_owner=owner,
                               module_sections=self.module_home() if owner else [])
        await self.slack.views_publish(user_id=user_id, view=view)

    async def on_home_opened(self, event: dict) -> None:
        if event.get("tab", "home") == "home" and event.get("user"):
            await self.publish_home(event["user"])

    async def on_home_action(self, body: dict) -> None:
        """App Home の更新とモジュールの項目。変えられるのは依頼者だけ。"""
        user = body.get("user", {}).get("id")
        if not self.is_allowed(user):
            return
        action = (body.get("actions") or [{}])[0]
        kind, _, name = action.get("action_id", "").partition(":")
        if kind == home.REFRESH_ACTION:
            pass  # 表示を作り直すだけ
        elif kind == home.MODULE_ACTION:
            module, _, item = name.partition(":")
            if not await self.module_home_action(module, item, action):
                return
        else:
            return
        await self.publish_home(user)
