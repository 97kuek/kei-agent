"""Slack につながないときの、Slack の代わり（知らせの置き場）。

Slack の受け口は頭（OpenAI Dots）が受け持ち、Kei Agent は Slack にいない（GitHub issue #17）。それでも本体と
モジュールは、ジョブが終わった・困りごと・課題の新着などを、これまでどおり「Slack に投稿する」つもりで書く。
その投稿を Slack に送らず、知らせとしてためる。頭は手の口の notices で読み、自分の名前で Slack に出す。

- chat_postMessage … 知らせを1件ためる（どのチャンネル・どのスレッドへのつもりだったかも残す）
- chat_update … ためた知らせの本文を書き換える（困りごとの知らせは、同じ1件を書き足していく）
- files_upload_v2 … 添付の中身を知らせに付ける
- チャンネルの一覧 … 研究テーマと、本体・モジュールのチャンネルの名前を、そのまま ID として返す
- リアクション・作業中の表示・App Home・入力の画面 … 何もしない（Slack にいないので見せる先がない）
"""

from __future__ import annotations

import itertools
import time

from kei_agent.storage.records import Records
from kei_agent.workspaces import themes

# 知らせを残す日数（頭が読んだあとも、この日数は recent などで見返せる）
KEEP_DAYS = 7
# 1件の知らせに付ける添付の長さ
FILE_CHARS = 20000
RECORDS = "outbox"
KIND = "notice"


class OutboxError(RuntimeError):
    """Slack にいないので、できない操作（流して見せる返事など）。呼び出し側は今までどおりの形に戻る。"""


class Outbox:
    def __init__(self, config, store):
        self.config = config
        self.records = Records(store, RECORDS)
        self._seq = itertools.count(1)

    # 投稿

    def _ts(self) -> str:
        return f"{time.time():.6f}{next(self._seq) % 1000:03d}"

    async def chat_postMessage(self, *, channel: str, text: str = "", markdown_text: str = "",
                               thread_ts: str | None = None, blocks: list | None = None, **_) -> dict:
        ts = self._ts()
        body = markdown_text or text or _blocks_text(blocks)
        self.records.put(KIND, ts, {"id": ts, "channel": channel, "thread_ts": thread_ts or "", "text": body,
                                    "at": time.time(), "delivered": False}, keep_days=KEEP_DAYS)
        return {"ok": True, "ts": ts, "channel": channel}

    async def chat_update(self, *, channel: str, ts: str, text: str = "", markdown_text: str = "",
                          blocks: list | None = None, **_) -> dict:
        body = markdown_text or text or _blocks_text(blocks)
        # 書き換えた知らせは、頭がもう読んでいても、もう一度読ませる
        self.records.update(KIND, ts, text=body, at=time.time(), delivered=False)
        return {"ok": True, "ts": ts, "channel": channel}

    async def files_upload_v2(self, *, channel: str = "", thread_ts: str = "", file_uploads: list | None = None,
                              **_) -> dict:
        for upload in file_uploads or []:
            content = str(upload.get("content") or "")
            name = upload.get("filename") or upload.get("title") or "file"
            await self.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                        text=f"📎 {name}\n```\n{content[:FILE_CHARS]}\n```")
        return {"ok": True}

    # 頭が読む

    def pending(self) -> list[dict]:
        """まだ頭に渡していない知らせ（古い順）。スレッドへの知らせには、親の知らせの本文を添える。"""
        found = [item for item in self.records.items(KIND) if not item.get("delivered")]
        found.sort(key=lambda item: item["at"])
        for item in found:
            parent = self.records.get(KIND, item["thread_ts"]) if item.get("thread_ts") else None
            item["thread"] = (parent or {}).get("text", "")
        return found

    def mark_delivered(self, ids: list[str]) -> None:
        for notice_id in ids:
            self.records.update(KIND, notice_id, delivered=True)

    # チャンネル

    def channel_names(self) -> list[str]:
        config = self.config
        names = [*config.overview_channels, *config.improve_channels,
                 *(name for names in config.module_channels.values() for name in names), *themes.all_themes(config)]
        return [name for name in dict.fromkeys(names) if "*" not in name]

    async def conversations_list(self, **_) -> dict:
        channels = [{"id": name, "name": name, "is_member": True} for name in self.channel_names()]
        return {"ok": True, "channels": channels, "response_metadata": {"next_cursor": ""}}

    async def conversations_info(self, channel: str, **_) -> dict:
        return {"ok": True, "channel": {"id": channel, "name": channel}}

    async def conversations_replies(self, **_) -> dict:
        return {"ok": True, "messages": []}

    async def chat_getPermalink(self, channel: str = "", message_ts: str = "", **_) -> dict:
        return {"ok": True, "permalink": ""}

    async def auth_test(self) -> dict:
        return {"ok": True, "user_id": "", "url": "", "team_id": ""}

    # Slack にいないので、何もしないもの

    async def reactions_add(self, **_) -> dict:
        return {"ok": True}

    async def reactions_remove(self, **_) -> dict:
        return {"ok": True}

    async def views_publish(self, **_) -> dict:
        return {"ok": True}

    async def views_open(self, **_) -> dict:
        return {"ok": True}

    async def views_update(self, **_) -> dict:
        return {"ok": True}

    async def assistant_threads_setStatus(self, **_) -> dict:
        raise OutboxError("Slack にいません")

    async def agents_sessions_setStatus(self, **_) -> dict:
        raise OutboxError("Slack にいません")

    async def chat_startStream(self, **_) -> dict:
        raise OutboxError("Slack にいません")

    async def chat_appendStream(self, **_) -> dict:
        raise OutboxError("Slack にいません")

    async def chat_stopStream(self, **_) -> dict:
        raise OutboxError("Slack にいません")


def _blocks_text(blocks: list | None) -> str:
    """ボタンなどの blocks の投稿の、読める文の部分。"""
    parts = []
    for block in blocks or []:
        text = block.get("text")
        if isinstance(text, dict) and text.get("text"):
            parts.append(str(text["text"]))
        for element in block.get("elements") or []:
            if isinstance(element, dict) and isinstance(element.get("text"), dict) and element["text"].get("text"):
                parts.append(str(element["text"]["text"]))
    return "\n".join(parts)
