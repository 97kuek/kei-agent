"""本体から来た出来事を、喋る文と顔に変える。

本体が渡すのは「何が起きたか」だけ（`{"kind": "done", "theme": "amr-query"}`）。
文と顔と首をここで組み立てる。本体に「どんな顔をさせるか」を持たせると、対応表が2か所に散る
（docs/voice.md の4節）。

声の言い方は、Slack に出す形とは別に作る。帯（`` `9時 .####...` ``）も URL も声では読めず、
「あと23時間で締切」は声なら「明日の夕方までだよ」の方が自然。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 表情は stackchan-atama が受け付ける6種だけ。`angry` は使わない（Kei Agent が怒る場面がない）
NEUTRAL, HAPPY, SAD, DOUBT, SLEEPY = "neutral", "happy", "sad", "doubt", "sleepy"
# 依頼を受けただけでは喋らない（依頼のたびに喋るとうるさい）
SILENT_KINDS = ("working", "schedule")
KINDS = ("schedule", "due", "working", "done", "failed", "limited", "awaiting")


@dataclass(frozen=True)
class Reaction:
    """出来事への反応。`text` が空なら喋らない（顔だけ変える）。"""
    text: str = ""
    face: str = NEUTRAL
    # 依頼者の方を向くか。気づいてほしいときだけ True にする
    look: bool = False

    @property
    def speaks(self) -> bool:
        return bool(self.text.strip())


def _theme(event: dict) -> str:
    name = str(event.get("theme") or "").strip()
    return f"{name} に頼んだ作業" if name else "さっきの作業"


def _hhmm(value: str) -> str:
    """声で読める時刻に。読めなければ空文字。"""
    try:
        at = datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return ""
    return f"{at.hour}時" if at.minute == 0 else f"{at.hour}時{at.minute}分"


def _due(event: dict) -> str:
    title = str(event.get("title") or "課題").strip()
    at = _hhmm(str(event.get("at") or ""))
    when = f"明日の{at}" if at else "明日"
    return f"{title}の締切、{when}までだよ。"


def reaction(event: dict) -> Reaction | None:
    """出来事への反応。知らない kind は None（黙って何もしない）。"""
    kind = str(event.get("kind") or "")
    if kind not in KINDS:
        return None
    if kind == "schedule":
        # 手元に置くだけ（速い道で使う）。喋らない
        return Reaction(face=NEUTRAL)
    if kind == "working":
        return Reaction(face=NEUTRAL)
    if kind == "due":
        return Reaction(_due(event), NEUTRAL, look=True)
    if kind == "done":
        return Reaction(f"{_theme(event)}、終わったよ。結果は Slack に出てる。", HAPPY, look=True)
    if kind == "failed":
        return Reaction(f"{_theme(event)}、うまくいかなかったみたい。Slack を見てみて。", SAD, look=True)
    if kind == "limited":
        at = _hhmm(str(event.get("reset_at") or ""))
        when = f"{at}ごろ" if at else "しばらくしたら"
        return Reaction(f"Claude の上限に当たっちゃった。{when}に自動でやり直すね。", SLEEPY)
    # awaiting
    return Reaction(f"{_theme(event)}、聞きたいことがあって止まってるよ。", DOUBT, look=True)


def summary(events: list[dict]) -> str:
    """離席中に溜まった知らせを、1回にまとめる（1件ずつ喋ると、離席が長いほど喋り続ける）。"""
    done = sum(1 for e in events if e.get("kind") == "done")
    failed = sum(1 for e in events if e.get("kind") == "failed")
    parts = []
    if done:
        parts.append(f"{done}件終わった")
    if failed:
        parts.append(f"{failed}件うまくいかなかった")
    if not parts:
        return ""
    return f"離れている間に、{'、'.join(parts)}よ。詳しくは Slack を見てね。"
