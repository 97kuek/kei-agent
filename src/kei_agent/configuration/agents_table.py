"""担当の表（利用者のフォルダの agents.csv）。モジュールのオンオフ・チャンネル・AI の実行器とモデルを1か所で変える。

1行に1つ（モジュールか、本体の router・overview）。列は module, enabled, channels, folder, notion, engine, model, effort,
claude_account, codex_account, claude_email（folder・notion・engines と、アカウントの3つは無くてもよい）。

- enabled … true / false（大文字でもよい）。表に無いモジュールはオフ
- channels … 番号を外したチャンネルの名前。複数は空白で区切る。空欄なら module.toml の既定
- folder … その担当の作業場（研究はテーマのフォルダを置く場所）。AI を持つ担当だけ。空欄なら既定
- notion … その担当が届く Notion のホームのページ（URL の末尾32文字）。overview の行は共通ホーム。空欄なら Notion を使わない
- engine … claude / codex。空欄は「まだ選んでいない」
- engines … 頭（Dots など）が選んでよい AI（空白で区切る。例 claude codex）。空欄なら engine の1つだけ
- model / effort … 空欄なら module.toml の用途ごとの選び分け。書けば、その担当の用途をすべてそのモデルにする
  （依頼者が明示したときだけの用途は除く）。使えるモデルは framework.models の一覧の中だけ
- claude_account / codex_account … その担当が使うアカウントのフォルダ（CLAUDE_CONFIG_DIR・CODEX_HOME。大学は個人、
  仕事は会社、など）。空欄ならプロセスの既定のアカウント
- claude_email … そのフォルダにログインしているはずの人（メールアドレス）。書けば、Claude を動かす前に確かめ、
  違うアカウントなら止めて知らせる（ログインの入れ替えで、会社と個人のアカウントが混ざらないように）

config.toml には modules・[channels]・[agents]・[notion] を書かない（書いてあれば、移すよう知らせて止める）。
研究と大学の置き場所も、この表の folder に書く（config.toml の research_root・course_root は使わない）。
表が無ければ、組み込みのモジュールを全部使い、AI は未選択。
読んだ中身は、config.toml と同じ形（modules・channels・agents・notion と、folders）にして load_config に渡す。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from kei_agent.configuration.csv_table import TableError, parse_bool, read_text, rows
from kei_agent.framework import modules

AGENTS_FILE = "agents.csv"
COLUMNS = ("module", "enabled", "channels", "folder", "notion", "engine", "engines", "model", "effort",
           "claude_account", "codex_account", "claude_email")
# 無くてもよい列（あとから足した列。前の表もそのまま読める）
OPTIONAL_COLUMNS = ("folder", "notion", "engines", "claude_account", "codex_account", "claude_email")
# 本体の行。router は振り分けの AI、overview は研究全体のチャンネル
ROUTER = "router"
OVERVIEW = "overview"
CORE_ROWS = (ROUTER, OVERVIEW)
# config.toml に書けないもの（この表に書く）
REPLACED_KEYS = ("modules", "channels", "agents", "notion")
# notion の列の行と、config.toml の [notion] の名前（それ以外のモジュールは [notion.homes]）
NOTION_KEYS = {OVERVIEW: "hub_home", "research": "research_home", "course": "course_home"}
# 研究と大学の置き場所の config.toml での名前（書いてあれば止める。この表の folder に書く）
FOLDER_KEYS = {"research_root": "research", "course_root": "course"}
def _channels(value: str) -> list[str]:
    return [name.lstrip("#") for name in value.split() if name.lstrip("#")]


def channel_kind(spec: modules.ModuleSpec) -> str | None:
    """その行の channels が受け持つチャンネルの種類。本体のチャンネル（improve）か、module.toml の [channels] の1つ。"""
    kinds = [*spec.core_channels, *spec.channels]
    return kinds[0] if len(kinds) == 1 else None


def parse(text: str, name: str = AGENTS_FILE) -> dict:
    """表を、config.toml と同じ形の {"modules": [...], "channels": {...}, "agents": {...}} にする。

    モデルが使えるものかは、ここでは見ない（config の [agents] を読むところで確かめる）。
    """
    known = modules.known()
    enabled: list[str] = []
    channels: dict[str, list[str]] = {}
    agents: dict[str, dict[str, str]] = {}
    folders: dict[str, str] = {}
    notion: dict = {"homes": {}}
    seen: set[str] = set()
    for line, row in rows(text, name, COLUMNS, OPTIONAL_COLUMNS,
                          too_many="（チャンネルを複数書くときは , ではなく空白で区切る）"):
        module = row["module"]
        where = f"{name} の {line} 行目（{module}）"
        if module in seen:
            raise TableError(f"{where}: {module} の行が2つあります")
        seen.add(module)
        if module not in known and module not in CORE_ROWS:
            raise TableError(f"{where}: 知らないモジュールです（知っているもの: {', '.join(sorted([*known, *CORE_ROWS]))}）")
        names = _channels(row["channels"])
        on = parse_bool(row["enabled"], where)
        engine, model, effort, folder = row["engine"], row["model"], row["effort"], row.get("folder", "")
        accounts = {key: row.get(key, "") for key in ("claude_account", "codex_account", "claude_email", "engines")
                    if row.get(key, "")}
        has_ai = module == ROUTER or (module in known and known[module].actor is not None)
        if folder and (module in CORE_ROWS or not has_ai):
            raise TableError(f"{where}: folder を書けるのは、AI を使う担当の行だけです")
        if accounts and not has_ai:
            raise TableError(f"{where}: claude_account・codex_account・claude_email・engines を書けるのは、"
                             "AI を使う担当の行だけです")
        if folder:
            folders[module] = folder
        if home := row.get("notion", ""):
            if module in (ROUTER, "notion"):
                raise TableError(f"{where}: この行には notion を書けません（共通ホームは overview の行）")
            if module in NOTION_KEYS:
                notion[NOTION_KEYS[module]] = home
            else:
                notion["homes"][module] = home
        if module == OVERVIEW:
            if not on:
                raise TableError(f"{where}: 研究全体のチャンネルはオフにできません（enabled は true）")
            if engine or model or effort:
                raise TableError(f"{where}: 研究全体のチャンネルには engine・model・effort を書けません")
            if names:
                channels[OVERVIEW] = names
            continue
        if module == ROUTER:
            if not on:
                raise TableError(f"{where}: 振り分け（router）はオフにできません（enabled は true）")
            if names:
                raise TableError(f"{where}: 振り分け（router）にはチャンネルを書けません")
        else:
            spec = known[module]
            if on:
                enabled.append(module)
            if names:
                kind = channel_kind(spec)
                if kind is None:
                    why = "チャンネルの種類が複数あるので、ここには書けません" if spec.channels else "チャンネルを持ちません"
                    raise TableError(f"{where}: このモジュールは{why}")
                channels[kind] = names
            if spec.actor is None:
                if engine or model or effort:
                    raise TableError(f"{where}: このモジュールは AI を使わないので、engine・model・effort は空欄にしてください")
                continue
        if (model or effort) and not engine:
            raise TableError(f"{where}: model や effort を書くときは engine も書いてください")
        if effort and not model:
            raise TableError(f"{where}: effort を書くときは model も書いてください（空欄なら用途ごとの既定）")
        agents[module] = {"provider": engine, "model": model, "effort": effort, **accounts}
    return {"modules": enabled, "channels": channels, "agents": agents, "folders": folders, "notion": notion}


def load(path: Path) -> dict:
    return parse(read_text(path), path.name)


def with_enabled(text: str, name: str, on: bool) -> str:
    """name の行の enabled だけを書き換えた表。行が無ければ、末尾に足す（ほかの列は空欄）。"""
    rows = list(csv.reader(io.StringIO(text)))
    header = [column.strip() for column in rows[0]] if rows else list(COLUMNS)
    at = {column: i for i, column in enumerate(header)}
    value = "true" if on else "false"
    for row in rows[1:]:
        if row and row[at["module"]].strip() == name:
            row[at["enabled"]] = value
            break
    else:
        new = [""] * len(header)
        new[at["module"]], new[at["enabled"]] = name, value
        rows = [header, *rows[1:], new] if rows else [header, new]
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def build(enabled: list[str] | None = None, *, channels: dict[str, list[str]] | None = None,
          folders: dict[str, str] | None = None, notion: dict[str, str] | None = None,
          providers: dict[str, str] | None = None) -> str:
    """表を作る（setup・モジュールの追加・テストの道具が使う）。知っているモジュールはどれも行にする。

    - enabled … オンにするモジュール（並べた順に行にする）。None なら組み込みを全部
    - channels … チャンネルの種類（module.toml の [channels] の名前か overview）→ チャンネルの名前
    - folders・notion・providers … 行の名前 → folder・notion・engine の列（notion の共通ホームは overview の行）
    """
    channels, folders, notion, providers = channels or {}, folders or {}, notion or {}, providers or {}
    known = modules.known()
    on = list(modules.builtin()) if enabled is None else enabled
    out = io.StringIO()
    writer = csv.DictWriter(out, COLUMNS, lineterminator="\n")
    writer.writeheader()

    def row(name: str, is_on: bool, names: object, actor: bool) -> None:
        writer.writerow({"module": name, "enabled": "true" if is_on else "false", "channels": " ".join(names or ()),
                         "folder": str(folders.get(name, "")), "notion": str(notion.get(name, "")),
                         "engine": providers.get(name, "") if actor else ""})

    row(ROUTER, True, (), True)
    row(OVERVIEW, True, channels.get(OVERVIEW, ()), False)
    for name in [*on, *sorted(set(known) - set(on))]:
        spec = known.get(name)
        if spec is None:
            # 知らないモジュールも行にする（読むときに、どの行が違うかを知らせる）
            writer.writerow({"module": name, "enabled": "true"})
            continue
        kind = channel_kind(spec)
        row(name, name in on, channels.get(kind, ()) if kind else (), spec.actor is not None)
    return out.getvalue()
