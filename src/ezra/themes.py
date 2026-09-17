"""チャンネルとテーマ、作業用ディレクトリの対応。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ezra.config import Config

OVERVIEW_DIR = "_overview"
THEME_SUBDIRS = ("inputs", "outputs", "logs", "papers")

CLAUDE_MD_TEMPLATE = """# テーマ: {name}

Slack の #{channel} チャンネルに対応する作業用ディレクトリ。Ezra（Slack Bot）がここで作業する。

## 研究の前提

<!-- 研究分野、目的、いま検証していること、使っているデータやモデルを書く -->

## 検索キーワード

<!-- 毎朝 07:00 に、ここに書いたキーワードで arXiv の新着を探す。1行に1つ、英語で書く（例: - vision language model counting） -->

## ディレクトリ

- `inputs/`: Slack で渡されたファイル（CSV など）
- `outputs/`: 図や集計結果。ここに新しくできたファイルは Slack のスレッドに添付される
- `logs/`: ジョブのログ
- `papers/`: 文献調査で見つけた論文のメタデータと要約

## ジョブにする基準

- 数分以上かかりそうな処理は、その場で実行せず `ezra:job` skill でジョブにする
- それより短い処理は、その場で実行してよい
"""

OVERVIEW_CLAUDE_MD = """# 研究全体・中長期の方針

Slack の研究全体と中長期の方針のチャンネルに対応する作業用ディレクトリ。
各テーマのディレクトリ（`../<theme>/`）は読むだけにし、書き込みはこのディレクトリの中だけにする。
"""


class ChannelKind(Enum):
    THEME = "theme"
    OVERVIEW = "overview"
    IMPROVE = "improve"
    # Ezra の対象外（inbox など）。何もしない
    OTHER = "other"


@dataclass(frozen=True)
class Workspace:
    channel_name: str
    kind: ChannelKind
    # claude -p を動かすディレクトリ。IMPROVE と OTHER では None
    cwd: Path | None
    # テーマの名前（チャンネル名から接頭辞を除いたもの）。ディレクトリと Notion のテーマに使う
    theme: str | None = None


# Slack のチャンネル名は日本語も使えるので、パスとして危ない形だけを弾く
_SAFE_NAME = re.compile(r"^[^./_\\\x00][^/\\\x00]{0,79}$")


def resolve(config: Config, channel_name: str) -> Workspace:
    if channel_name in config.improve_channels:
        return Workspace(channel_name, ChannelKind.IMPROVE, None)
    if channel_name in config.overview_channels:
        return Workspace(channel_name, ChannelKind.OVERVIEW, config.research_root / OVERVIEW_DIR)
    prefix = config.theme_channel_prefix
    if not channel_name.startswith(prefix):
        return Workspace(channel_name, ChannelKind.OTHER, None)
    theme = channel_name[len(prefix):]
    if not _SAFE_NAME.match(theme) or ".." in theme:
        raise ValueError(f"テーマ名に使えないチャンネル名です: {channel_name!r}")
    return Workspace(channel_name, ChannelKind.THEME, config.research_root / theme, theme)


def theme_channel_name(config: Config, theme: str) -> str:
    """テーマの名前から、Slack のチャンネル名を作る。"""
    return f"{config.theme_channel_prefix}{theme}"


def other_channel_message(config: Config) -> str:
    return (
        f"このチャンネルは Ezra の対象外です。研究テーマとして使うときは、"
        f"チャンネル名を `{config.theme_channel_prefix}<テーマ名>` にしてください。"
    )


def ensure_workspace(ws: Workspace) -> bool:
    """作業用ディレクトリとひな形を作る。新しく作ったら True。"""
    if ws.cwd is None:
        return False
    created = not ws.cwd.exists()
    ws.cwd.mkdir(parents=True, exist_ok=True)
    claude_md = ws.cwd / "CLAUDE.md"
    if ws.kind is ChannelKind.THEME:
        for sub in THEME_SUBDIRS:
            (ws.cwd / sub).mkdir(exist_ok=True)
        if not claude_md.exists():
            claude_md.write_text(CLAUDE_MD_TEMPLATE.format(name=ws.theme, channel=ws.channel_name), encoding="utf-8")
    elif ws.kind is ChannelKind.OVERVIEW:
        (ws.cwd / "outputs").mkdir(exist_ok=True)
        if not claude_md.exists():
            claude_md.write_text(OVERVIEW_CLAUDE_MD, encoding="utf-8")
    return created
