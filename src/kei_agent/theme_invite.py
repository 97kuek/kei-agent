"""研究テーマのチャンネルに招かれたとき、フォルダの置き場所を聞く（docs/agents/research-agent.md の「テーマの作業場」）。

まだフォルダの無いテーマなら [既定の場所に作る] [既存のフォルダを使う] と聞く。既存のフォルダは入力の画面で受け取り、
使えるか確かめてから themes.toml に書き足す。何も選ばずに頼まれたら、既定の場所に作る（今までどおり）。

Assistant に混ぜて使う。self.slack、self.config などは Assistant のもの。
"""

from __future__ import annotations

import json
import logging

from kei_agent import themes
from kei_agent.slack_text import escape
from kei_agent.themes import ChannelKind, PlaceError, Workspace

log = logging.getLogger(__name__)

DEFAULT_ACTION = "kei_agent_theme_place_default"
EXISTING_ACTION = "kei_agent_theme_place_existing"
SUBMIT_CALLBACK = "kei_agent_theme_place_submit"
FOLDER_BLOCK = "folder"


def choice_blocks(ws: Workspace, channel: str) -> list[dict]:
    """置き場所の選び方（ボタン2つ）。研究テーマとプロジェクトのチャンネルで使う。"""
    what = "テーマ" if ws.kind is ChannelKind.THEME else "プロジェクト"
    # 毎晩 Git に保存するのは研究のフォルダ（~/research）だけ
    saved = "（毎晩 Git に保存する）" if ws.kind is ChannelKind.THEME else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Kei Agent です。{what}「{escape(ws.channel_name)}」のフォルダを決めてね。"}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": DEFAULT_ACTION, "value": channel, "style": "primary",
             "text": {"type": "plain_text", "text": "既定の場所に作る"}},
            {"type": "button", "action_id": EXISTING_ACTION, "value": channel,
             "text": {"type": "plain_text", "text": "既存のフォルダを使う"}},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"既定の場所は `{ws.cwd}`{saved}。既存のフォルダやリポジトリも使える"
            "（そのフォルダの Git はあなたの管理のまま）")}]},
    ]


def folder_modal(channel: str, message_ts: str) -> dict:
    """既存のフォルダの場所を入れる画面。"""
    return {
        "type": "modal",
        "callback_id": SUBMIT_CALLBACK,
        "private_metadata": json.dumps({"channel": channel, "message_ts": message_ts}),
        "title": {"type": "plain_text", "text": "既存のフォルダを使う"},
        "submit": {"type": "plain_text", "text": "使う"},
        "close": {"type": "plain_text", "text": "やめる"},
        "blocks": [{
            "type": "input", "block_id": FOLDER_BLOCK,
            "label": {"type": "plain_text", "text": "フォルダの場所"},
            "element": {"type": "plain_text_input", "action_id": FOLDER_BLOCK,
                        "placeholder": {"type": "plain_text", "text": "~/src/my-repo"}},
            "hint": {"type": "plain_text", "text": "この Mac のフォルダ。AGENTS.md か CLAUDE.md があれば、そのまま前提として使う"},
        }],
    }


class ThemeInvite:
    async def ask_theme_place(self, channel: str, ws: Workspace) -> None:
        """まだフォルダの無い研究テーマに招かれた。置き場所を聞く。"""
        what = "テーマ" if ws.kind is ChannelKind.THEME else "プロジェクト"
        await self.slack.chat_postMessage(channel=channel, text=f"{what}「{ws.channel_name}」のフォルダを決めてね",
                                          blocks=choice_blocks(ws, channel))

    async def on_theme_place_action(self, body: dict) -> None:
        """[既定の場所に作る] [既存のフォルダを使う] が押された。変えられるのは依頼者だけ。"""
        if not self.is_allowed(body.get("user", {}).get("id")):
            return
        action = (body.get("actions") or [{}])[0]
        channel = str(action.get("value") or "")
        message_ts = str((body.get("container") or {}).get("message_ts") or (body.get("message") or {}).get("ts") or "")
        if not channel:
            return
        if action.get("action_id") == EXISTING_ACTION:
            await self.slack.views_open(trigger_id=body.get("trigger_id"), view=folder_modal(channel, message_ts))
            return
        ws = themes.resolve(self.config, await self.channel_name(channel))
        if ws.kind not in (ChannelKind.THEME, ChannelKind.PROJECT):
            return
        await self._settle_theme(channel, message_ts, ws, f"既定の場所 `{ws.cwd}` に作ったよ")

    async def on_theme_place_submit(self, body: dict) -> dict | None:
        """既存のフォルダの場所が送られた。使えなければ、欄の下に理由を出す（画面は閉じない）。"""
        if not self.is_allowed(body.get("user", {}).get("id")):
            return {FOLDER_BLOCK: "依頼者だけが決められます"}
        view = body.get("view") or {}
        meta = json.loads(view.get("private_metadata") or "{}")
        channel, message_ts = str(meta.get("channel") or ""), str(meta.get("message_ts") or "")
        value = (((view.get("state") or {}).get("values") or {}).get(FOLDER_BLOCK) or {}).get(FOLDER_BLOCK) or {}
        try:
            folder = themes.check_place(self.config, str(value.get("value") or ""))
            name = themes.theme_name(await self.channel_name(channel))
            themes.save_place(self.config, name, folder)
        except PlaceError as e:
            return {FOLDER_BLOCK: str(e)[:150]}
        ws = themes.resolve(self.config, name)
        notes = [f"`{folder}` を使うね（themes.toml に書いたよ）"]
        if (folder / ".git").is_dir():
            notes.append("Kei Agent の記録は `.kei-agent/` にまとめて、このリポジトリの Git には入れないようにしたよ")
        notes.append("毎晩の保存はしないので、このフォルダの Git はあなたの管理のまま")
        if warning := themes.icloud_warning(folder):
            notes.append(f"⚠️ {warning}")
        await self._settle_theme(channel, message_ts, ws, "\n".join(notes))
        return None

    async def _settle_theme(self, channel: str, message_ts: str, ws: Workspace, done: str) -> None:
        """フォルダを用意して、テーマを登録し、選んだ結果を伝える（ボタンは消す）。"""
        had_notes = ws.cwd is not None and any((ws.cwd / name).exists() for name in (themes.NOTES_FILE, themes.CLAUDE_FILE))
        themes.ensure_workspace(ws)
        self.registered_themes.add(ws.channel_name)
        if ws.kind is ChannelKind.THEME:
            # 研究ホームのテーマの行（プロジェクトは Notion に載せない）
            await self.register_theme(channel, ws)
        if message_ts:
            try:
                await self.slack.chat_update(channel=channel, ts=message_ts, text=done, blocks=[])
            except Exception:
                log.warning("置き場所の選び方の投稿を書き換えられませんでした", exc_info=True)
        found = themes.notes_file(ws.cwd).name if ws.cwd is not None else themes.NOTES_FILE
        what = "研究" if ws.kind is ChannelKind.THEME else "プロジェクト"
        premise = (f"前からある `{found}` を、{what}の前提として使うね。" if had_notes
                   else f"{what}の前提を `AGENTS.md` に書いておくと、依頼のたびに説明しなくて済みます。")
        await self.slack.chat_postMessage(channel=channel, text=f"{done}\n{premise}")
