"""速い道。Kei Agent がすでに答えを持っているものは、モデルを通さずに答える。

本体が変わったときに押しておいたもの（朝のまとめ、作業の様子）から作る。聞かれてから
取りに行かない。予定と締切は朝に決まって、日中ほとんど変わらない（docs/voice.md の3節）。

振り分けに軽いモデルを置くと、**速い道の全部に往復が乗る**ので、言葉で当てる。
外したら遅い道（Codex）が答えるので、当てが雑でも壊れない。
"""

from __future__ import annotations

from datetime import datetime

# 何を聞かれたか。上から順に見る
SCHEDULE_WORDS = ("予定", "スケジュール", "きょうは何", "今日は何")
NEXT_WORDS = ("次は", "つぎは", "このあと", "この後")
DUE_WORDS = ("締切", "しめきり", "締め切り", "期限", "いつまで", "課題")
CLASS_WORDS = ("授業", "講義", "何限")
MEETING_WORDS = ("会議", "打ち合わせ", "ミーティング", "予定入ってる")
STATUS_WORDS = ("どんな感じ", "進んでる", "動いてる", "終わった", "状況")
TIME_WORDS = ("何時", "いま何時", "今何時")

CLASS, MEETING, DUE = "🎓", "💼", "⏰"


def _spoken_time(hhmm: str) -> str:
    """`10:40` → 「10時40分」。声で読める形にする。"""
    try:
        hour, _, minute = hhmm.partition(":")
        h, m = int(hour), int(minute)
    except ValueError:
        return hhmm
    return f"{h}時" if m == 0 else f"{h}時{m}分"


def _line(item: dict) -> str:
    return f"{_spoken_time(str(item.get('at') or ''))}から{item.get('text') or ''}"


def _items(held: dict, icons: tuple[str, ...] = ()) -> list[dict]:
    found = (held.get("schedule") or {}).get("items") or []
    return [i for i in found if not icons or i.get("icon") in icons]


def _upcoming(items: list[dict], now: datetime) -> list[dict]:
    at = f"{now:%H:%M}"
    return [i for i in items if str(i.get("at") or "") >= at]


def answer(text: str, held: dict, now: datetime | None = None) -> str | None:
    """聞かれたことに、手元のもので答える。答えられなければ None（遅い道へ）。"""
    now = now or datetime.now()
    asked = text.strip()
    if not asked:
        return None

    if any(w in asked for w in TIME_WORDS):
        return f"いま{_spoken_time(f'{now:%H:%M}')}だよ。"

    if any(w in asked for w in STATUS_WORDS):
        return _status(held)

    if any(w in asked for w in DUE_WORDS):
        return _listed(_items(held, (DUE,)), now, "締切", "締切は入ってないよ。")
    if any(w in asked for w in CLASS_WORDS):
        return _listed(_items(held, (CLASS,)), now, "授業", "今日の授業はもう無いよ。")
    if any(w in asked for w in MEETING_WORDS):
        return _listed(_items(held, (MEETING,)), now, "会議", "会議の予定は無いよ。")

    if any(w in asked for w in NEXT_WORDS):
        rest = _upcoming(_items(held), now)
        return f"次は{_line(rest[0])}だよ。" if rest else "このあとの予定は無いよ。"

    if any(w in asked for w in SCHEDULE_WORDS):
        return _listed(_items(held), now, "予定", "今日はもう予定が無いよ。")
    return None


def closed(text: str) -> bool:
    """付け足す余地のない問いか。

    時刻だけは、答えたあとに Codex へ「ひとことある？」と聞く意味がない（毎回往復が乗るだけ）。
    予定や締切は「なら先にこっちをやった方がいい」のような続きがありうるので、聞く。
    """
    return any(w in text for w in TIME_WORDS)


def _listed(items: list[dict], now: datetime, what: str, empty: str) -> str:
    rest = _upcoming(items, now)
    if not rest:
        return empty
    if len(rest) == 1:
        return f"{_line(rest[0])}だよ。"
    body = "、".join(_line(i) for i in rest[:3])
    more = f"。ほかに{len(rest) - 3}件あるよ" if len(rest) > 3 else ""
    return f"このあとの{what}は、{body}{more}。"


def _status(held: dict) -> str:
    """いま Kei Agent がどうなっているか。押されてきた出来事から数える。"""
    running = held.get("running") or 0
    done = held.get("done") or 0
    failed = held.get("failed") or 0
    if held.get("limited"):
        return "いま Claude の上限に当たってて、止まってるよ。"
    parts = []
    if running:
        parts.append(f"{running}件動いてる")
    if done:
        parts.append(f"{done}件終わった")
    if failed:
        parts.append(f"{failed}件うまくいかなかった")
    return f"今日は{'、'.join(parts)}よ。" if parts else "いまは何も動いてないよ。"
