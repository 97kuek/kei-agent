"""チャンネルとテーマ、作業用ディレクトリの対応。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kei_agent import modules
from kei_agent.config import Config

THEME_SUBDIRS = ("inputs", "outputs", "logs")

CLAUDE_MD_TEMPLATE = """# テーマ: {name}

Slack の #{name} チャンネルに対応する作業用ディレクトリ。Kei Agent（Slack Bot）がここで作業する。

## 研究の前提

<!-- 研究分野、目的、いま検証していること、使っているデータやモデルを書く -->

## 検索キーワード

<!-- 毎朝 07:00 に、ここに書いたキーワードで arXiv の新着を探す（知識の担当が、この前提と見比べて選ぶ）。1行に1つ、英語で書く（例: - vision language model counting） -->

## ディレクトリ

- `inputs/`: Slack で渡されたファイル（CSV など）
- `outputs/`: 図や集計結果。ここに新しくできたファイルは Slack のスレッドに添付される
- `logs/`: ジョブのログ
- 論文は、研究ホームの「先行研究」DB に残す（このディレクトリには置かない）

## ジョブにする基準

- 数分以上かかりそうな処理は、その場で実行せず `kei-agent-research:running-jobs` skill でジョブにする
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
    # モジュールのチャンネル（module.toml の [channels]）。そのモジュールの module.py に取り次ぐだけで、ファイルは持たない
    MODULE = "module"
    # Kei Agent 自身を直すときの worktree（improve.py）。書き込めるのはその中だけ
    SELF_FIX = "self_fix"
    # 振り分け・分類の係（router.py）。何も書かず、材料はプロンプトで渡す
    ROUTER = "router"


@dataclass(frozen=True)
class Workspace:
    channel_name: str
    kind: ChannelKind
    # claude -p を動かすディレクトリ。IMPROVE では None
    cwd: Path | None
    # config.toml の基本の接続先に足して、このテーマで許可した接続先（Slack で許可したもの。settings.py）
    allowed_domains: tuple[str, ...] = ()
    # ここから下は、エージェントが自分の claude を動かすときの上乗せ（docs/architecture.md の「振り分けと A2A」）
    # そのエージェントの指示書（prompts/<agent>.md）。既定は prompts/system.md
    system_prompt: Path | None = None
    # claude 1回の上限時間（分）。既定は config.run_timeout_minutes
    timeout_minutes: int | None = None
    # MODULE のときの、モジュールの名前
    module: str = ""
    # 指示書の最後に、依頼者のプロフィールを差し込むか（JSON だけを返す振り分け・分類・選別の係は差し込まない）
    profile: bool = True


# Slack のチャンネル名は日本語も使えるので、パスとして危ない形だけを弾く
_SAFE_NAME = re.compile(r"^[^./_\\\x00][^/\\\x00]{0,79}$")
# チャンネル名の先頭の番号（`10_amr-query` の `10_`）。並び順のためのもので、名前の一部として扱わない
_NUMBER_PREFIX = re.compile(r"^\d{2,}_")


def theme_name(channel_name: str) -> str:
    """チャンネル名から、テーマの名前（フォルダ名、Notion のテーマ名）を作る。

    Slack では並び順のために `10_amr-query` のような番号を付ける。番号を変えても
    同じテーマを指し続けられるよう、先頭の番号は外して扱う。
    """
    return _NUMBER_PREFIX.sub("", channel_name)


def resolve(config: Config, channel_name: str) -> Workspace:
    """チャンネル名から作業場所を決める。研究全体・改善・モジュール以外は、すべて研究テーマとして扱う。

    ほかのどれにも当たらないチャンネルを受け持つモジュール（[channels] に "*"）があれば、テーマはそのモジュールのもの
    （Workspace.module）。無ければ本体の研究。
    """
    channel_name = theme_name(channel_name)
    if channel_name in config.improve_channels:
        return Workspace(channel_name, ChannelKind.IMPROVE, None)
    module = module_of_channel(config, channel_name)
    if module:
        return Workspace(channel_name, ChannelKind.MODULE, None, module=module)
    if channel_name in config.overview_channels:
        return Workspace(channel_name, ChannelKind.OVERVIEW, config.overview_dir)
    if not _SAFE_NAME.match(channel_name) or ".." in channel_name:
        raise ValueError(f"テーマ名に使えないチャンネル名です: {channel_name!r}")
    return Workspace(channel_name, ChannelKind.THEME, config.research_root / channel_name,
                     module=catch_all_module(config))


def module_of_channel(config: Config, channel_name: str) -> str:
    """そのチャンネルを持つ、オンのモジュールの名前（設定の [channels] で変えた名前も見る）。無ければ空文字。"""
    for spec in modules.enabled(config.modules):
        if any(channel_name in config.module_channels.get(kind, ()) and channel_name != modules.ALL_CHANNELS
               for kind in spec.channels):
            return spec.name
    return ""


def catch_all_module(config: Config) -> str:
    """ほかのどれにも当たらないチャンネル（研究テーマ）を受け持つ、オンのモジュールの名前。無ければ空文字（本体の研究）。"""
    for spec in modules.enabled(config.modules):
        if any(config.module_channels.get(kind, ()) == (modules.ALL_CHANNELS,) for kind in spec.channels):
            return spec.name
    return ""


# チャンネルの種類ごとに、会話を続ける担当（研究テーマと研究全体は研究の担当、モジュールはそのモジュール）
_ACTORS = {ChannelKind.IMPROVE: "self_fix"}


def actor_of(ws: Workspace) -> str:
    """そのチャンネルで会話を続ける担当。モジュールのチャンネルと、モジュールが受け持つ研究テーマは、そのモジュール。"""
    if ws.module and ws.kind in (ChannelKind.MODULE, ChannelKind.THEME):
        return ws.module
    return _ACTORS.get(ws.kind, "research")


def agent_workspace(config: Config, agent: str) -> Workspace:
    """モジュールのエージェントが AI を動かす場所。会話の続きは作業場ごとに残るので、毎回同じ場所にする。

    module.toml の [actor] workspace（大学は ~/course。前提のメモの CLAUDE.md を置く）か、状態の置き場の下
    （`agents/<名前>`）。どれも手元のファイルは作業場を読むだけ（制限の表）。
    """
    spec = modules.known().get(agent)
    if spec is not None and spec.actor is not None:
        ws = Workspace(agent, ChannelKind.MODULE, config.module_workspace(agent), module=agent)
    else:
        raise ValueError(f"作業場を持たないエージェントです: {agent}")
    ensure_workspace(ws)
    return ws


def theme_dirs(config: Config) -> list[Path]:
    """研究テーマの作業用ディレクトリ。

    「どれがテーマか」の判断は resolve() に合わせる（2か所で別々に決めない）。
    """
    root = config.research_root
    if not root.is_dir():
        return []
    dirs = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith((".", "_")):
            continue
        try:
            if resolve(config, p.name).kind is ChannelKind.THEME:
                dirs.append(p)
        except ValueError:
            continue
    return dirs


def search_keywords(claude_md: Path) -> list[str]:
    """テーマの CLAUDE.md の「## 検索キーワード」の箇条書きを読む。"""
    if not claude_md.exists():
        return []
    text = re.sub(r"<!--.*?-->", "", claude_md.read_text(encoding="utf-8"), flags=re.DOTALL)
    m = re.search(r"^## 検索キーワード\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return [line.strip()[2:].strip() for line in m.group(1).splitlines()
            if line.strip().startswith("- ") and line.strip()[2:].strip()]


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
            claude_md.write_text(CLAUDE_MD_TEMPLATE.format(name=ws.channel_name), encoding="utf-8")
    elif ws.kind is ChannelKind.OVERVIEW:
        (ws.cwd / "outputs").mkdir(exist_ok=True)
        if not claude_md.exists():
            claude_md.write_text(OVERVIEW_CLAUDE_MD, encoding="utf-8")
    elif ws.kind is ChannelKind.MODULE and ws.module and not claude_md.exists():
        # モジュールの作業場のひな形（大学なら、履修の補足と覚えておいてほしいことを書く場所）
        template = modules.known()[ws.module].path / modules.WORKSPACE_TEMPLATE
        if template.is_file():
            claude_md.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return created
