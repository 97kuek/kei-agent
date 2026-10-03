"""Dot に渡す通知を保存する。MCP notices の取得後、配信成功の確認応答で配信済みにする。"""

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
            name = upload.get("title") or upload.get("filename") or "file"
            if upload.get("content") is not None:
                body = f"```\n{str(upload['content'])[:FILE_CHARS]}\n```"
            else:
                # 作業場にできたファイル（outputs/）。頭は read_file で中身を読める
                body = f"作業場の `{name}` にできたよ（文のファイルなら read_file で読める）"
            await self.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"📎 {name}\n{body}")
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
                 *(name for names in config.module_channels.values() for name in names),
                 *themes.all_themes(config), *themes.all_projects(config)]
        return [name for name in dict.fromkeys(names) if "*" not in name]

    async def conversations_list(self, **_) -> dict:
        channels = [{"id": name, "name": name, "is_member": True} for name in self.channel_names()]
        return {"ok": True, "channels": channels, "response_metadata": {"next_cursor": ""}}

    async def conversations_info(self, channel: str, **_) -> dict:
        return {"ok": True, "channel": {"id": channel, "name": channel}}



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
