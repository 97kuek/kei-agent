"""Realtime API に渡す道具（docs/architecture.md の「声のレイヤ」）。

モデルは依頼者のことを何も知らない。予定・締切・研究テーマは**こちらから道具として渡す**。
渡さないかぎり、何を聞かれても答えられない。

**これは前の作りの直し**でもある。前は言葉で当てていて（`予定` が入っていたら今日の予定を返す）、
「明日の予定」「今週の予定」「今日は何やったんだっけ」に**全部今日の予定を答えていた**（実測）。
道具にすれば、いつのことかはモデルが引数で渡してくる。

| 道具 | 何をするか | どこから取るか |
|---|---|---|
| `get_schedule` | 予定と締切（日をまたげる） | 本体が押しておいたもの（`executor.held`） |
| `get_status` | いま動いている依頼・終わった数・上限 | 同じ |
| `ask_agent` | 研究・授業・仕事の中身を調べる | 選択済み provider の担当 agent。数秒かかる |
| `propose_request` | 作業の依頼を**下書きする**（まだ渡さない） | — |
| `send_request` | 下書きを Kei Agent に渡す | `<state_dir>/asks/` |

**依頼は2段にする。** 下書きしてから渡す形にしておけば、1回の思い違いで作業が動き出さない。
読み上げて確認するのはモデルの仕事（`live.py` の指示に書く）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from kei_agent import ask as asks
from kei_agent.config import Config
from kei_agent_voice.executor import current
from kei_agent_voice.handoff import Handoff

log = logging.getLogger(__name__)

CLASS, MEETING, DUE = "🎓", "💼", "⏰"
# 声で読む都合で、1回に読み上げる件数は絞る（10件並べても聞き取れない）
MAX_ITEMS = 6

# モデルに渡す道具の形（`session.update` の `tools`）
DEFINITIONS = [
    {
        "type": "function",
        "name": "get_schedule",
        "description": "依頼者の予定・授業・会議・締切を返す。いつのことか分からないときは today。",
        "parameters": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "week"],
                    "description": "today は今日の残り、tomorrow は明日、week は今日から7日間",
                },
                "kind": {
                    "type": "string",
                    "enum": ["all", "class", "meeting", "due"],
                    "description": "絞りたいとき。既定は all",
                },
            },
            "required": ["day"],
        },
    },
    {
        "type": "function",
        "name": "get_status",
        "description": "Kei Agent がいま何をしているか（動いている依頼、終わった数、契約の上限）を返す。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "ask_agent",
        "description": ("依頼者の研究・授業・仕事の中身を調べて答える。ファイルを読むので数秒かかる。"
                        "調べている間、依頼者には「ちょっと見てみるね」と言っておくこと。"
                        "**調べる相手はこの会話を聞いていない**ので、question は"
                        "それだけ読めば分かる一文にする（「さっきのあれ」と書かない）。"),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["research", "course", "work"],
                          "description": "調べる担当"},
                "question": {"type": "string", "description": "調べてほしいことを、一文で"},
                "theme": {"type": "string", "description": "研究ならテーマ名（例 amr-query）"},
            },
            "required": ["agent", "question"],
        },
    },
    {
        "type": "function",
        "name": "propose_request",
        "description": ("Kei Agent に作業を頼む前の下書きを作る。**これだけでは何も動かない。**"
                        "返ってきた文をそのまま読み上げて、依頼者が「いいよ」と言ってから send_request を呼ぶ。"),
        "parameters": {
            "type": "object",
            "properties": {
                "theme": {"type": "string", "description": "テーマ名（Slack のチャンネル名。例 amr-query）"},
                "text": {"type": "string", "description": "依頼の文。そのまま Slack のスレッドに載る"},
            },
            "required": ["theme", "text"],
        },
    },
    {
        "type": "function",
        "name": "send_request",
        "description": "読み上げて確認した下書きを、Kei Agent に渡す。確認していないときは呼ばない。",
        "parameters": {"type": "object", "properties": {}},
    },
]


@dataclass
class Draft:
    theme: str
    text: str


class Tools:
    """道具の中身。`held` は本体が押してきたもの（`executor.held`）。"""

    def __init__(self, held: dict, config: Config, handoff: Handoff | None = None):
        self.held = held
        self.config = config
        # 担当への問い合わせは本体に頼む（担当を呼べるのは本体だけ）
        self.handoff = handoff or Handoff(config)
        self.draft: Draft | None = None

    async def call(self, name: str, arguments: dict, now: datetime | None = None) -> str:
        """道具を呼ぶ。**返すのは、モデルが読み上げられる短い文**。

        知らない道具や失敗は、例外にせず文で返す（会話を止める方が悪い）。
        ループの上で呼ぶ（ask_agent は待つあいだループを止めない）。
        """
        now = now or datetime.now()
        try:
            if name == "get_schedule":
                return self.get_schedule(str(arguments.get("day") or "today"),
                                          str(arguments.get("kind") or "all"), now)
            if name == "get_status":
                return self.get_status(now)
            if name == "ask_agent":
                return await self.ask_agent(str(arguments.get("agent") or "research"),
                                      str(arguments.get("question") or ""),
                                      str(arguments.get("theme") or ""))
            if name == "propose_request":
                return self.propose_request(str(arguments.get("theme") or ""),
                                             str(arguments.get("text") or ""))
            if name == "send_request":
                return self.send_request()
        except Exception as e:
            log.exception("道具でつまずきました: %s", name)
            return f"うまくいかなかった（{type(e).__name__}）。Slack で頼んでみて。"
        return f"{name} という道具は持っていない。"

    # 手元にあるもの（本体が押しておいたもの）

    def get_schedule(self, day: str, kind: str = "all", now: datetime | None = None) -> str:
        now = now or datetime.now()
        icons = {"class": (CLASS,), "meeting": (MEETING,), "due": (DUE,)}.get(kind, ())
        items = [i for i in self._items() if not icons or i.get("icon") in icons]
        wanted = _within(items, day, now)
        if not wanted:
            return f"{_day_word(day)}は、{_kind_word(kind)}が入っていない。"
        lines = [_line(i, day, now) for i in wanted[:MAX_ITEMS]]
        more = f"（ほかに{len(wanted) - MAX_ITEMS}件）" if len(wanted) > MAX_ITEMS else ""
        return f"{_day_word(day)}の{_kind_word(kind)}: " + "、".join(lines) + more

    def get_status(self, now: datetime | None = None) -> str:
        current(self.held, now)
        if self.held.get("limited"):
            return "いま Claude の上限に当たっていて、止まっている。"
        running = int(self.held.get("running") or 0)
        done = int(self.held.get("done") or 0)
        failed = int(self.held.get("failed") or 0)
        if not (running or done or failed):
            return "いまは何も動いていない。"
        parts = []
        if running:
            parts.append(f"{running}件動いている")
        if done:
            parts.append(f"{done}件終わった")
        if failed:
            parts.append(f"{failed}件うまくいかなかった")
        return "今日は" + "、".join(parts) + "。"

    def _items(self) -> list[dict]:
        found = (self.held.get("schedule") or {}).get("items") or []
        return [i for i in found if isinstance(i, dict)]

    # 調べる（時間がかかる）

    async def ask_agent(self, actor: str, question: str, theme: str = "") -> str:
        if not question.strip():
            return "何を調べるか分からなかった。"
        return await self.handoff.ask(actor, question, theme)

    # 依頼（下書き → 渡す）

    def propose_request(self, theme: str, text: str) -> str:
        theme, text = theme.strip().lstrip("#"), text.strip()
        if not theme or not text:
            return "テーマと依頼の文の両方が要る。"
        self.draft = Draft(theme, text)
        return f"下書きした。これをそのまま読み上げて確認して: 「{theme} に、{text}、って頼むよ。いい？」"

    def send_request(self) -> str:
        draft, self.draft = self.draft, None
        if draft is None:
            return "渡すものが無い。先に propose_request を呼んで、読み上げて確認して。"
        asks.write_ask(self.config, draft.theme, draft.text)
        log.info("声から依頼を渡しました: %s / %s", draft.theme, draft.text[:60])
        return f"{draft.theme} に渡した。終わったら知らせが来る。"


def _day_word(day: str) -> str:
    return {"tomorrow": "明日", "week": "今週", "today": "今日"}.get(day, "今日")


def _kind_word(kind: str) -> str:
    return {"class": "授業", "meeting": "会議", "due": "締切"}.get(kind, "予定")


def _date_of(item: dict, now: datetime) -> date:
    """その予定の日。日付が書かれていなければ今日のものとして扱う。"""
    written = str(item.get("date") or "")
    try:
        return date.fromisoformat(written)
    except ValueError:
        return now.date()


def _within(items: list[dict], day: str, now: datetime) -> list[dict]:
    """聞かれた範囲のものだけ。今日は**これから**のぶんだけ返す。"""
    today = now.date()
    if day == "tomorrow":
        wanted = [i for i in items if _date_of(i, now) == today + timedelta(days=1)]
    elif day == "week":
        wanted = [i for i in items if today <= _date_of(i, now) <= today + timedelta(days=7)]
    else:
        at = f"{now:%H:%M}"
        wanted = [i for i in items
                  if _date_of(i, now) == today and str(i.get("at") or "") >= at]
    return sorted(wanted, key=lambda i: (_date_of(i, now), str(i.get("at") or "")))


def _line(item: dict, day: str, now: datetime) -> str:
    when = str(item.get("at") or "")
    text = str(item.get("text") or "")
    if day == "week":
        return f"{_date_of(item, now):%m月%d日} {when} {text}".strip()
    return f"{when} {text}".strip()
