"""エージェントの返事の形（全エージェント共通の封筒）。docs/agents.md

    {"ok": true, "text": "人が読む文", "data": {}, "limit_reset_at": null, "cost_usd": 0.02}

- `text` … オーケストレーターがそのまま Slack に出せる文
- `data` … 組み立て直したいときに使う中身（締切の一覧、claude の実行結果など）
- `limit_reset_at` … Claude の契約の上限に当たったときの、明ける時刻（エポック秒）。
  依頼者への約束（「◯時ごろにやり直す」）はオーケストレーターが1か所で持つので、ここで返すだけにする
- `cost_usd` … その仕事でかかった額（分かるときだけ）

スキルごとに形が違うと、エージェントが増えるたびに受け取り側の分岐が増える。封筒は揃える。
"""

from __future__ import annotations

import json

FIELDS = ("ok", "text", "data", "limit_reset_at", "cost_usd")


def reply(text: str = "", data: dict | None = None, ok: bool = True,
          limit_reset_at: float | None = None, cost_usd: float | None = None) -> str:
    """封筒に詰めた JSON 文字列。"""
    return json.dumps({
        "ok": ok,
        "text": text,
        "data": data or {},
        "limit_reset_at": limit_reset_at,
        "cost_usd": cost_usd,
    }, ensure_ascii=False)


def failure(text: str, data: dict | None = None) -> str:
    """できなかったことを返す封筒（A2A のタスクは failed にする）。"""
    return reply(text, data, ok=False)
