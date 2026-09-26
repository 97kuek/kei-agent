"""Slack に表示してよい Kei Agent の最終出力だけを扱う。"""

from __future__ import annotations

import re

FINAL_OPEN = "<<kei-agent-final>>"
FINAL_CLOSE = "<<kei-agent-final-end>>"

DAILY_HEADINGS = (
    "**今日のタスク**",
    "**夜間処理の結果**",
    "**確認待ち・期日・止まっているテーマ・返事待ち**",
    "**今日考えるとよい問い**",
)
REVIEW_HEADINGS = ("**今日の成果**", "**未完了タスク**")
NIGHT_QUESTION = "夜間に実行したいタスクはありますか？"

_LOCAL_PATH = re.compile(r"(?:file://\S+|~/(?:\S+)|(?:^|[\s([{\"])\/(?:\S+))")
_WEB_URL = re.compile(r"https?://\S+")
_RELATIVE_LOCAL_PATH = re.compile(
    r"(?:\b(?:reviews|overview|outputs|inputs|logs|\.kei-agent|\.config|\.local)/\S+"
    r"|\b(?:[\w.-]+/)+[\w.-]+\.(?:md|txt|json|csv|py|html|pdf|xlsx?|docx?))"
)
_INTERNAL_PROGRESS = re.compile(
    r"\b(?:Bash|Read|Glob|Grep|WebSearch|WebFetch|Skill|Codex App|Claude Code|apply_patch|pytest|ruff)\b"
    r"|(?:材料|参照スレッド|スレッド).{0,24}(?:確認|読[みむ])"
    r"|(?:まず|次に|最後に).{0,32}(?:確認|調査|作成|実装|実行|進め)"
    r"|(?:調査|作業|処理).{0,12}(?:中です|します|を開始)",
    re.IGNORECASE,
)
_INTERNAL_EXCEPTION = re.compile(r"\b(?:Traceback|[A-Za-z][\w.]*(?:Error|Exception))\b")


class OutputError(ValueError):
    """モデルの文が Slack 出力契約を満たさない。"""


def _contains_local_path(text: str) -> bool:
    """Web URL は残し、手元の絶対パス（file URI、~/、/...）だけを見つける。

    テーマの作業用ディレクトリの中の相対パス（``outputs/fig.png`` など）は、
    プロンプトがファイル名を書くよう頼んでいるので拒否しない。
    """
    return bool(_LOCAL_PATH.search(_WEB_URL.sub("", text)))


def _forbidden(text: str) -> bool:
    return bool(_contains_local_path(text) or _INTERNAL_PROGRESS.search(text))


# 置き換えるときのパス。読点・句点・括弧・引用符でパスが終わったとみなす
_PATH_CHARS = r"[^\s、。，,)）\]}」』>`'\"]"
_HIDE_LOCAL_PATH = re.compile(rf"(?:file://|~/|(?<![\w.:/-])/){_PATH_CHARS}+")


def _hide_local_paths(text: str) -> str:
    """Web URL を避けて、手元の絶対パスをファイル名だけに置き換える。"""
    def hide(match: re.Match[str]) -> str:
        return match.group(0).rstrip("/").rsplit("/", 1)[-1]

    pieces, last = [], 0
    for url in _WEB_URL.finditer(text):
        pieces += [_HIDE_LOCAL_PATH.sub(hide, text[last:url.start()]), url.group(0)]
        last = url.end()
    pieces.append(_HIDE_LOCAL_PATH.sub(hide, text[last:]))
    return "".join(pieces)


def finalize_conversation(text: str) -> str:
    """final marker 内だけを利用者向け本文として採用する。

    marker で最終回答を切り出すので、本文の言い回しでは捨てない。手元の絶対パスだけは
    返答ごと捨てずにファイル名へ置き換える。
    """
    normalized = text.strip().replace("\r\n", "\n")
    if normalized.count(FINAL_OPEN) != 1 or normalized.count(FINAL_CLOSE) != 1:
        raise OutputError("final marker がちょうど1組ではない")
    _before, marked = normalized.split(FINAL_OPEN, 1)
    body, _close, tail = marked.partition(FINAL_CLOSE)
    if tail.strip():
        raise OutputError("final marker の後ろに文がある")
    if not body.strip():
        raise OutputError("final marker の中が空")
    return _hide_local_paths(body.strip())


def validate_structured_response(text: str) -> str:
    """定型 A2A の、コードで組み立てた返答を Slack に出せる形か確かめる。

    自由文のモデル応答は ``finalize_conversation`` を必ず通す。一方、締切や
    同期結果のような定型処理は A2A の ``data`` と固定の整形で表示するため、
    marker を要求せず、内部経過・例外・パスだけを拒否する。
    """
    normalized = text.strip().replace("\r\n", "\n")
    if (not normalized or FINAL_OPEN in normalized or FINAL_CLOSE in normalized
            or _forbidden(normalized) or _INTERNAL_EXCEPTION.search(normalized)):
        raise OutputError("structured response contract")
    return normalized


def _heading_name(line: str) -> str:
    """見出しらしい行から、飾り（`#`、太字の `*`、末尾のコロン）を外した名前。"""
    return line.strip().lstrip("#").strip().strip("*").strip().rstrip(":：").strip()


def _validate_sections(text: str, headings: tuple[str, ...], message: str) -> str:
    """決まった見出しがこの順で1回ずつあり、どれも中身がある返答だけを受け入れる。

    見出しの書き方（`###` や末尾のコロン）と、見出しや項目のあいだの空行の違いでは捨てず、
    見出しは決まった書き方にそろえる。最初の見出しより前に文があるもの、手元のパスや
    作業の実況を含むものは捨てる。
    """
    normalized = text.strip().replace("\r\n", "\n")
    names = [heading.strip("*") for heading in headings]
    lines = normalized.split("\n")
    marks = [(index, _heading_name(line)) for index, line in enumerate(lines) if _heading_name(line) in names]
    if [name for _, name in marks] != names or marks[0][0] != 0 or _forbidden(normalized):
        raise OutputError(message)
    parts = []
    ends = [index for index, _ in marks[1:]] + [len(lines)]
    for (start, name), end in zip(marks, ends, strict=True):
        body = "\n".join(lines[start + 1:end]).strip()
        if not body:
            raise OutputError(message)
        parts.append(f"**{name}**\n{body}")
    return "\n\n".join(parts)


def validate_daily(text: str) -> str:
    """Daily の四つの section だけを受け入れる。"""
    return _validate_sections(text, DAILY_HEADINGS, "Daily の返答が指定形式ではありません")


def validate_review(text: str) -> str:
    """Retro の二つの section と夜間質問だけを受け入れる。夜間質問は決まった文なので、無ければ足す。"""
    normalized = text.strip().replace("\r\n", "\n")
    if normalized.endswith(NIGHT_QUESTION):
        normalized = normalized[:-len(NIGHT_QUESTION)].rstrip()
    return _validate_sections(
        normalized, REVIEW_HEADINGS, "Retro の返答が指定形式ではありません"
    ) + "\n\n" + NIGHT_QUESTION


def safe_failure(kind: str = "conversation") -> str:
    """内部詳細を含めない、Slack 用の固定失敗文を返す。"""
    messages = {
        "conversation": "⚠️ 返答を利用者向けの形に整えられなかったよ。もう一度頼んでね。",
        "daily": "⚠️ Daily を利用者向けの形に整えられなかったよ。あとでもう一度実行するね。",
        "review": "⚠️ 振り返りを利用者向けの形に整えられなかったよ。もう一度頼んでね。",
        "connection": "⚠️ 接続に失敗したよ。少し時間を置いてもう一度頼んでね。",
        "timeout": "⚠️ 時間がかかりすぎたよ。少し時間を置いてもう一度頼んでね。",
        "provider": "⚠️ 使う AI（Claude か Codex）がまだ選ばれていないよ。App Home の設定で選んでからもう一度頼んでね。",
    }
    return messages.get(kind, messages["conversation"])


def trouble_notice(text: str, limit: int = 200) -> str:
    """改善チャンネルに出す、何が起きたかの1行。

    `…できません: RuntimeError: …` のように後ろに付く例外の中身は出さない（ログにだけ残す）。
    手元の絶対パスは名前だけにし、長さを切る。
    """
    head = re.split(r": |：", text.strip(), maxsplit=1)[0]
    first = " ".join(_INTERNAL_EXCEPTION.sub("", _hide_local_paths(head)).split())
    return first if len(first) <= limit else first[:limit - 1] + "…"
