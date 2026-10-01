"""担当の表（利用者のフォルダの agents.csv）。モジュールのオンオフ・チャンネル・AI の実行器とモデルを1か所で変える。

1行に1つ（モジュールか、本体の router・overview）。列は module, enabled, channels, engine, model, effort。

- enabled … true / false（大文字でもよい）。表に無いモジュールはオフ
- channels … 番号を外したチャンネルの名前。複数は空白で区切る。空欄なら module.toml の既定
- engine … claude / codex。空欄は「まだ選んでいない」
- model / effort … 空欄なら module.toml の用途ごとの選び分け。書けば、その担当の用途をすべてそのモデルにする
  （依頼者が明示したときだけの用途は除く）。使えるモデルは model_policy の一覧の中だけ

この表があるときは、config.toml の modules・[channels]・[agents] を書かない（同じことを2か所に書かない）。
読んだ中身は、config.toml と同じ形（modules・channels・agents）にして load_config に渡す。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from kei_agent import modules

AGENTS_FILE = "agents.csv"
COLUMNS = ("module", "enabled", "channels", "engine", "model", "effort")
# 本体の行。router は振り分けの AI、overview は研究全体のチャンネル
ROUTER = "router"
OVERVIEW = "overview"
CORE_ROWS = (ROUTER, OVERVIEW)
# この表があるときに config.toml に書けないもの
REPLACED_KEYS = ("modules", "channels", "agents")
_TRUE = {"true": True, "false": False}


class TableError(ValueError):
    pass


def _bool(value: str, where: str) -> bool:
    found = _TRUE.get(value.strip().lower())
    if found is None:
        raise TableError(f"{where} の enabled は true か false にしてください: {value!r}")
    return found


def _channels(value: str) -> list[str]:
    return [name.lstrip("#") for name in value.split() if name.lstrip("#")]


def channel_kind(spec: modules.ModuleSpec) -> str | None:
    """その行の channels が受け持つチャンネルの種類。本体のチャンネル（improve）か、module.toml の [channels] の1つ。"""
    kinds = [*spec.core_channels, *spec.channels]
    return kinds[0] if len(kinds) == 1 else None


def read_text(path: Path) -> str:
    try:
        # Excel が付ける BOM は外す
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise TableError(f"{path.name} を UTF-8 として読めません。UTF-8（CSV UTF-8）で保存し直してください") from None


def parse(text: str, name: str = AGENTS_FILE) -> dict:
    """表を、config.toml と同じ形の {"modules": [...], "channels": {...}, "agents": {...}} にする。

    モデルが使えるものかは、ここでは見ない（config の [agents] を読むところで確かめる）。
    """
    reader = csv.DictReader(io.StringIO(text))
    header = [column.strip() for column in reader.fieldnames or ()]
    if sorted(header) != sorted(COLUMNS):
        raise TableError(f"{name} の1行目（列の名前）は {','.join(COLUMNS)} にしてください（今は {','.join(header) or '空'}）")
    reader.fieldnames = header
    known = modules.known()
    enabled: list[str] = []
    channels: dict[str, list[str]] = {}
    agents: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        if None in raw:
            raise TableError(f"{name} の {line} 行目は列が多すぎます（チャンネルを複数書くときは , ではなく空白で区切る）")
        row = {key: (value or "").strip() for key, value in raw.items()}
        module = row["module"]
        if not module or module.startswith("#"):
            continue
        where = f"{name} の {line} 行目（{module}）"
        if module in seen:
            raise TableError(f"{where}: {module} の行が2つあります")
        seen.add(module)
        if module not in known and module not in CORE_ROWS:
            raise TableError(f"{where}: 知らないモジュールです（知っているもの: {', '.join(sorted([*known, *CORE_ROWS]))}）")
        names = _channels(row["channels"])
        on = _bool(row["enabled"], where)
        engine, model, effort = row["engine"], row["model"], row["effort"]
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
        agents[module] = {"provider": engine, "model": model, "effort": effort}
    return {"modules": enabled, "channels": channels, "agents": agents}


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


def from_config(data: dict, providers: dict[str, str] | None = None) -> str:
    """config.toml の modules・[channels]・[agents] から、同じ中身の表を作る（移すとき）。
    providers は今使っている provider（App Home で選んだもの）。あれば [agents] より先に使う。"""
    providers = providers or {}
    known = modules.known()
    on = data.get("modules", list(modules.builtin()))
    channels = data.get("channels", {})
    agents = data.get("agents", {})
    if "self_fix" in agents and "improve" not in agents:
        # 前の名前（[agents.self_fix]）は、今の名前で書き出す
        agents = {**agents, "improve": agents["self_fix"]}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)

    def row(name: str, enabled: bool, names: object, actor: bool) -> None:
        provider = (providers.get(name) or str(agents.get(name, {}).get("provider", ""))) if actor else ""
        writer.writerow([name, "true" if enabled else "false", " ".join(names or ()), provider, "", ""])

    row(ROUTER, True, (), True)
    row(OVERVIEW, True, channels.get(OVERVIEW, ()), False)
    for name in [*on, *sorted(set(known) - set(on))]:
        spec = known.get(name)
        if spec is None:
            continue
        kind = channel_kind(spec)
        row(name, name in on, channels.get(kind, ()) if kind else (), spec.actor is not None)
    return out.getvalue()
