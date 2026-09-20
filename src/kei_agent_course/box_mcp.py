"""claude に Box を読む道具を渡す（MCP サーバー、標準入出力）。

大学エージェントが claude を動かすときに、この3つだけを差す（読み取り専用）。

- `box_search` … 名前と中身で探す
- `box_read` … 本文をテキストで読む（入っていなければページを画像で返す）
- `box_pages` … ページや写真を画像で見る（スキャンした過去問など）

ファイルはローカルに保存しない。Box の鍵はこのプロセスだけが持ち、claude（の Bash）からは読めない場所に置く
（`docs/agents.md`）。

    kei-agent-box-mcp   # claude が起動する。人が直接動かすものではない
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys

from mcp import types
from mcp.server.mcpserver import MCPServer

from kei_agent_course.box import Box, BoxError

log = logging.getLogger(__name__)

INSTRUCTIONS = ("Box に置いてある学部要項や過去問を読むための道具。読み取りだけで、書き込みや削除はできない。"
                "探してから読む。答えるときは、使ったファイルの名前と URL を書く。")
# 1回の検索で返す件数の上限（多すぎると読みきれない）
MAX_RESULTS = 20
MAX_PAGES = 3

server = MCPServer(name="box", instructions=INSTRUCTIONS)


def _box() -> Box:
    return Box.load()


def _image(data: bytes) -> types.ImageContent:
    return types.ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg")


@server.tool()
async def box_search(query: str, limit: int = 10, extensions: str = "") -> str:
    """Box のファイルを名前と中身で探す。

    Args:
        query: 探す言葉（日本語でよい。例: 情報セキュリティ 過去問）
        limit: 返す件数（1〜20）
        extensions: 拡張子で絞る（例: "pdf" や "pdf,jpg"）。空なら全部
    """
    try:
        found = await asyncio.to_thread(_box().search, query, min(max(limit, 1), MAX_RESULTS), extensions)
    except BoxError as e:
        return f"Box を探せませんでした: {e}"
    if not found:
        return f"「{query}」では見つかりませんでした。言葉を変えて探してみてください。"
    return json.dumps(found, ensure_ascii=False, indent=1)


@server.tool()
async def box_read(file_id: str) -> list[types.ContentBlock]:
    """Box のファイルの中身を読む。本文が入っていなければ、ページを画像で返す。

    Args:
        file_id: box_search が返した id
    """
    box = _box()
    try:
        text = await asyncio.to_thread(box.text, file_id)
        if text.strip():
            return [types.TextContent(type="text", text=text)]
        images = await asyncio.to_thread(box.page_images, file_id, MAX_PAGES)
    except BoxError as e:
        return [types.TextContent(type="text", text=f"Box を読めませんでした: {e}")]
    if not images:
        return [types.TextContent(type="text", text="このファイルは本文も画像も取り出せませんでした。")]
    return [types.TextContent(type="text", text=f"本文が入っていないので、ページの画像を {len(images)} 枚渡します。"),
            *[_image(data) for data in images]]


@server.tool()
async def box_pages(file_id: str, pages: int = 2) -> list[types.ContentBlock]:
    """Box のファイルのページを画像で見る（手書きやスキャンの過去問、写真）。

    Args:
        file_id: box_search が返した id
        pages: 何ページまで見るか（1〜3）
    """
    try:
        images = await asyncio.to_thread(_box().page_images, file_id, min(max(pages, 1), MAX_PAGES))
    except BoxError as e:
        return [types.TextContent(type="text", text=f"Box を読めませんでした: {e}")]
    if not images:
        return [types.TextContent(type="text", text="このファイルからは画像を取り出せませんでした。")]
    return [_image(data) for data in images]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
