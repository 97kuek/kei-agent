"""チャンネルとテーマ、作業用ディレクトリの対応。

研究テーマのフォルダは、既定では research_root/<テーマ>。既存のフォルダやリポジトリを使うテーマは、利用者のフォルダの
themes.toml に「テーマの名前 = フォルダ」と書く（docs/agents/research-agent.md の「テーマの作業場」）。ファイルが変わると
読み直すので、本体も担当プロセスも起動し直さなくてよい。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kei_agent.configuration.config import Config
from kei_agent.configuration.places import (  # noqa: F401  themes.toml とチャンネル名の決まりは設定の領域
    _SAFE_NAME,
    ICLOUD_DRIVE,
    PROTECTED_FOLDERS,
    THEMES_FILE,
    PlaceError,
    check_place,
    check_places,
    icloud_warning,
    places,
    save_place,
    theme_name,
    themes_file,
)
from kei_agent.framework import modules

log = logging.getLogger(__name__)

# 研究テーマの作業場に作るフォルダ
THEME_SUBDIRS = ("inputs", "outputs", "logs")
# Kei Agent の記録の置き場所。既存のリポジトリでは .git/info/exclude に入れて、その人の Git に混ぜない
STATE_DIR = ".kei-agent"

# 作業場の前提のメモ。正本は AGENTS.md（Codex が読む）で、CLAUDE.md はそれを読み込む1行（Claude Code が読む）
NOTES_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
CLAUDE_IMPORT = "@AGENTS.md\n"

THEME_NOTES = """# テーマ: {name}

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

PROJECT_NOTES = """# プロジェクト: {name}

Slack の #{name} チャンネルに対応する作業場。Kei Agent（Slack Bot）がここでコードを書き、コマンドを動かす。

## 前提

<!-- 何のプロジェクトか、言語とフレームワーク、テストの回し方、触ってはいけないところを書く -->
"""

OVERVIEW_NOTES = """# 研究全体・中長期の方針

Slack の研究全体と中長期の方針のチャンネルに対応する作業用ディレクトリ。
各テーマのディレクトリ（`../<theme>/`）は読むだけにし、書き込みはこのディレクトリの中だけにする。
"""


class ChannelKind(Enum):
    THEME = "theme"
    OVERVIEW = "overview"
    IMPROVE = "improve"
    # モジュールのチャンネル（module.toml の [channels]）。そのモジュールの module.py に取り次ぐだけで、ファイルは持たない
    MODULE = "module"
    # プロジェクトのチャンネル（[channels] に "work-*" のような頭）。研究テーマと同じく、チャンネルごとの作業場で
    # 担当がファイルを書き、コマンドを動かす。作業場はその担当のフォルダ（agents.csv の folder）の下の <頭を除いた名前>
    PROJECT = "project"
    # モジュールが自分のフォルダ（状態の置き場の modules/<名前>/ の中）で AI を動かすとき（core.run_ai の folder）。
    # 書き込めるのはその中だけ
    FOLDER = "folder"
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
    # MODULE・FOLDER のときの、モジュールの名前。研究テーマ・研究全体・Kei Agent のチャンネルでは、会話を受け持つモジュール
    module: str = ""
    # 指示書の最後に、依頼者のプロフィールを差し込むか（JSON だけを返す振り分け・分類・選別の係は差し込まない）
    profile: bool = True
    # 既存のフォルダを使う研究テーマ（themes.toml）。フォルダの中の決まりをなるべく変えない
    external: bool = False


def resolve(config: Config, channel_name: str) -> Workspace:
    """チャンネル名から作業場所を決める。研究全体・改善・モジュール以外は、すべて研究テーマとして扱う。

    ほかのどれにも当たらないチャンネルを受け持つモジュール（[channels] に "*"）があれば、テーマはそのモジュールのもの
    （Workspace.module）。無ければ本体の研究。
    """
    channel_name = theme_name(channel_name)
    if channel_name in config.improve_channels:
        # Kei Agent のチャンネル。会話を受け持つモジュール（core_channels に improve）があれば、そのモジュールが答える
        return Workspace(channel_name, ChannelKind.IMPROVE, None, module=core_channel_owner(config, "improve"))
    module = module_of_channel(config, channel_name)
    if module:
        return Workspace(channel_name, ChannelKind.MODULE, None, module=module)
    if channel_name in config.overview_channels:
        # 研究全体の相談は、研究テーマを受け持つモジュールの担当が、すべてのテーマを読んで答える
        return Workspace(channel_name, ChannelKind.OVERVIEW, config.overview_dir, module=catch_all_module(config))
    if not _SAFE_NAME.match(channel_name) or ".." in channel_name:
        raise ValueError(f"テーマ名に使えないチャンネル名です: {channel_name!r}")
    place = places(config).get(channel_name)
    module, project = project_of_channel(config, channel_name)
    if module:
        if not _SAFE_NAME.match(project) or ".." in project:
            raise ValueError(f"プロジェクト名に使えないチャンネル名です: {channel_name!r}")
        return Workspace(channel_name, ChannelKind.PROJECT, place or config.module_workspace(module) / project,
                         module=module, external=place is not None)
    return Workspace(channel_name, ChannelKind.THEME, place or config.research_root / channel_name,
                     module=catch_all_module(config), external=place is not None)


def module_of_channel(config: Config, channel_name: str) -> str:
    """そのチャンネルを持つ、オンのモジュールの名前（agents.csv で変えた名前も見る）。無ければ空文字。"""
    for spec in modules.enabled(config.modules):
        if any(channel_name in config.module_channels.get(kind, ()) and channel_name != modules.ALL_CHANNELS
               for kind in spec.channels):
            return spec.name
    return ""


def project_of_channel(config: Config, channel_name: str) -> tuple[str, str]:
    """頭が一致するチャンネル（プロジェクト）なら、受け持つオンのモジュールと、頭を除いた名前。違えば ("", "")。"""
    for spec in modules.enabled(config.modules):
        for kind in spec.channels:
            for pattern in config.module_channels.get(kind, ()):
                head = modules.channel_prefix(pattern)
                if head and channel_name.startswith(head) and len(channel_name) > len(head):
                    return spec.name, channel_name[len(head):]
    return "", ""


def core_channel_owner(config: Config, kind: str) -> str:
    """本体のチャンネル（Kei Agent のチャンネルなど）の会話を受け持つ、オンのモジュールの名前。無ければ空文字。"""
    return next((spec.name for spec in modules.enabled(config.modules) if kind in spec.core_channels), "")


def catch_all_module(config: Config) -> str:
    """ほかのどれにも当たらないチャンネル（研究テーマ）を受け持つ、オンのモジュールの名前。無ければ空文字（本体の研究）。"""
    for spec in modules.enabled(config.modules):
        if any(config.module_channels.get(kind, ()) == (modules.ALL_CHANNELS,) for kind in spec.channels):
            return spec.name
    return ""


def actor_of(ws: Workspace) -> str:
    """そのチャンネルで会話を続ける担当。モジュールのチャンネル・研究テーマ・研究全体・Kei Agent のチャンネルは、
    それを受け持つモジュール（無ければ空文字。そのチャンネルでは答えない）。振り分けの係などは担当を持たない。"""
    return ws.module


def agent_workspace(config: Config, agent: str) -> Workspace:
    """モジュールのエージェントが AI を動かす場所。会話の続きは作業場ごとに残るので、毎回同じ場所にする。

    module.toml の [actor] workspace（大学は ~/course。前提のメモの AGENTS.md を置く）か、状態の置き場の下
    （`agents/<名前>`）。どれも手元のファイルは作業場を読むだけ（制限の表）。
    """
    spec = modules.known().get(agent)
    if spec is not None and spec.actor is not None:
        ws = Workspace(agent, ChannelKind.MODULE, config.module_workspace(agent), module=agent)
    else:
        raise ValueError(f"作業場を持たないエージェントです: {agent}")
    ensure_workspace(ws)
    return ws


def all_themes(config: Config) -> dict[str, Path]:
    """研究テーマの名前と作業用ディレクトリ（既定の場所の下にあるものと、themes.toml のもの）。

    「どれがテーマか」の判断は resolve() に合わせる（2か所で別々に決めない）。
    """
    found: dict[str, Path] = {}
    root = config.research_root
    for p in sorted(root.iterdir()) if root.is_dir() else ():
        if not p.is_dir() or p.name.startswith((".", "_")):
            continue
        try:
            if resolve(config, p.name).kind is ChannelKind.THEME:
                found[p.name] = p
        except ValueError:
            continue
    for name, path in sorted(places(config).items()):
        # themes.toml には、プロジェクトのチャンネルの既存のフォルダも入る（テーマではない）
        if path.is_dir() and _is_theme(config, name):
            found[name] = path
    return found


def _is_theme(config: Config, name: str) -> bool:
    try:
        return resolve(config, name).kind is ChannelKind.THEME
    except ValueError:
        return False


def notes_file(folder: Path) -> Path:
    """作業場の前提のメモ。AGENTS.md（無くて CLAUDE.md だけがある既存のフォルダなら、その CLAUDE.md）。"""
    agents = folder / NOTES_FILE
    return agents if agents.exists() or not (folder / CLAUDE_FILE).exists() else folder / CLAUDE_FILE


def _ensure_notes(folder: Path, template: str | None, *, move: bool) -> None:
    """前提のメモを AGENTS.md にそろえ、CLAUDE.md はそれを読み込む1行にする。

    move なら、前からある CLAUDE.md を AGENTS.md に移す（Kei Agent が作った作業場だけ。既存のフォルダの
    CLAUDE.md は、その人のものなので動かさない）。どちらも無ければ、template で AGENTS.md を作る。
    """
    agents, claude = folder / NOTES_FILE, folder / CLAUDE_FILE
    if not agents.exists() and claude.exists():
        if not move or claude.read_text(encoding="utf-8").strip() == CLAUDE_IMPORT.strip():
            return
        claude.rename(agents)
    if not agents.exists() and template is not None:
        agents.write_text(template, encoding="utf-8")
    if agents.exists() and not claude.exists():
        claude.write_text(CLAUDE_IMPORT, encoding="utf-8")


def search_keywords(notes: Path) -> list[str]:
    """テーマの前提のメモ（AGENTS.md）の「## 検索キーワード」の箇条書きを読む。"""
    if not notes.exists():
        return []
    text = re.sub(r"<!--.*?-->", "", notes.read_text(encoding="utf-8"), flags=re.DOTALL)
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
    if ws.kind is ChannelKind.THEME:
        if ws.external:
            # 既存のフォルダ: inputs/ などは使うときに作る。記録の置き場所は、その人の Git に混ぜない
            (ws.cwd / STATE_DIR).mkdir(exist_ok=True)
            _exclude_state_dir(ws.cwd)
        else:
            for sub in THEME_SUBDIRS:
                (ws.cwd / sub).mkdir(exist_ok=True)
        # 前提のメモがあれば、そのまま使う。無いときだけひな形を作る
        _ensure_notes(ws.cwd, THEME_NOTES.format(name=ws.channel_name), move=not ws.external)
    elif ws.kind is ChannelKind.PROJECT:
        # プロジェクトはたいてい Git のリポジトリ。Kei Agent の記録と受け渡しのフォルダは、その人の Git に混ぜない
        (ws.cwd / STATE_DIR).mkdir(exist_ok=True)
        _exclude_state_dir(ws.cwd, ("inputs", "outputs"))
        _ensure_notes(ws.cwd, PROJECT_NOTES.format(name=ws.channel_name), move=not ws.external)
    elif ws.kind is ChannelKind.OVERVIEW:
        (ws.cwd / "outputs").mkdir(exist_ok=True)
        _ensure_notes(ws.cwd, OVERVIEW_NOTES, move=True)
    elif ws.kind is ChannelKind.MODULE and ws.module:
        # モジュールの作業場のひな形（大学なら、履修の補足と覚えておいてほしいことを書く場所）
        template = modules.known()[ws.module].path / modules.WORKSPACE_TEMPLATE
        _ensure_notes(ws.cwd, template.read_text(encoding="utf-8") if template.is_file() else None, move=True)
    return created


def _exclude_state_dir(folder: Path, extra: tuple[str, ...] = ()) -> None:
    """Git のリポジトリなら、Kei Agent の記録の置き場所（.kei-agent/）と extra のフォルダを .git/info/exclude に
    入れる（一度だけ）。"""
    git = folder / ".git"
    if not git.is_dir():
        return
    exclude = git / "info" / "exclude"
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = [line for line in (f"/{name}/" for name in (STATE_DIR, *extra)) if line not in current.splitlines()]
    if not lines:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(current + ("" if not current or current.endswith("\n") else "\n")
                       + "# Kei Agent の記録（スレッドの記録・ジョブの状態）と、受け渡しのフォルダ\n"
                       + "".join(f"{line}\n" for line in lines), encoding="utf-8")
