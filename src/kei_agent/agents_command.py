"""担当の表（`kei-agent agents init`）。config.toml の modules・[channels]・[agents] を agents.csv に移す。

- 今の設定と同じ中身の agents.csv を作り（AI は App Home で今選んでいるものを engine に書く）、config.toml から
  その3つを消す（ほかの行とコメントは残す。前の設定は config.toml.bak に残す）。書く前に、新しい設定を読めて、
  モジュール・チャンネル・AI が変わらないことを確かめる
- `--dry-run` で、作る表を見るだけ
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib

from kei_agent.configuration import agents_table
from kei_agent.configuration.config import (
    DEFAULT_PATHS,
    Config,
    ConfigError,
    _expand,
    config_home,
    config_path,
    load_config,
)
from kei_agent.framework import modules

_TABLE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]")
_MODULES = re.compile(r"^\s*(modules|research_root|course_root)\s*=")


def without_replaced(text: str) -> str:
    """config.toml から modules・research_root・course_root の行と、[channels]・[agents]・[agents.<名前>] の表を
    消した中身。"""
    out: list[str] = []

    skipping = False
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        row = lines[i]
        table = _TABLE.match(row)
        if table:
            name = table.group(1).strip()
            skipping = name in ("channels", "agents") or name.startswith("agents.")
        if skipping:
            i += 1
            continue
        if _MODULES.match(row) and not any(_TABLE.match(r) for r in lines[:i]):
            # 複数の行にまたがる配列（modules = [ …）は、閉じ括弧の行まで消す
            if "[" in lines[i].split("#", 1)[0]:
                while "]" not in lines[i].split("#", 1)[0] and i + 1 < len(lines):
                    i += 1
            i += 1
            continue
        out.append(row)
        i += 1
    return "".join(out)


def split(text: str, providers: dict[str, str] | None = None) -> tuple[str, str]:
    """modules・[channels]・[agents] を含む config.toml の中身を、(それを除いた config.toml, 同じ中身の agents.csv) に分ける。"""
    return without_replaced(text), agents_table.from_config(tomllib.loads(text), providers)


def mismatches(data: dict, providers: dict[str, str], after: Config) -> list[str]:
    """移す前の config.toml（data）と App Home の選択から決まるはずのものと、移したあとの設定の違い。"""
    found = []
    names = [n for n in dict.fromkeys(data.get("modules", list(modules.builtin())))]
    if list(after.modules) != names:
        found.append(f"モジュール {names} → {list(after.modules)}")
    for kind, values in data.get("channels", {}).items():
        now = {"overview": after.overview_channels, "improve": after.improve_channels}.get(
            kind, after.module_channels.get(kind, tuple(values)))
        if list(now) != list(values):
            found.append(f"チャンネル {kind} {values} → {list(now)}")
    for actor, profile in after.agent_profiles.items():
        want = providers.get(actor) or str(data.get("agents", {}).get(actor, {}).get("provider", ""))
        if profile.provider != want:
            found.append(f"AI {actor} {want or '未選択'} → {profile.provider or '未選択'}")
    return found


def init(*, env: dict[str, str] | None = None, dry_run: bool = False) -> int:
    env = dict(os.environ) if env is None else env
    path = config_path(env)
    home = config_home(env)
    table = home / agents_table.AGENTS_FILE
    if not path.is_file():
        print(f"❌ 設定ファイルが無い: {path}（先に kei-agent setup か、config.example.toml を写す）")
        return 1
    if table.exists():
        print(f"{table} はもうあります（書き換えない）")
        return 0
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
        modules.register_user_modules(home / "modules")
    except (tomllib.TOMLDecodeError, modules.ModuleError) as e:
        print(f"❌ 今の設定を読めない: {e}")
        return 1
    # App Home で選んだ AI（SQLite）。表に移したあとは起動のときに消えるので、表に書き写す
    from kei_agent.doctor import stored_providers

    state_dir = _expand(str(data.get("state_dir", DEFAULT_PATHS["state_dir"])))
    providers = stored_providers(state_dir / "kei-agent.db")
    new_text, csv_text = split(text, providers)
    print(csv_text)
    trial_toml = path.with_name(f".{path.name}.trial")
    trial_csv = table.with_name(f".{table.name}.trial")
    try:
        trial_toml.write_text(new_text, encoding="utf-8")
        trial_csv.write_text(csv_text, encoding="utf-8")
        after = load_config(path=trial_toml, env={**env, "KEI_AGENT_HOME": str(home)}, agents_csv=trial_csv)
    except ConfigError as e:
        print(f"❌ 移したあとの設定を読めないので、書き換えなかった: {e}")
        return 1
    finally:
        trial_toml.unlink(missing_ok=True)
        trial_csv.unlink(missing_ok=True)
    if found := mismatches(data, providers, after):
        print(f"❌ 移すと中身が変わってしまうので、書き換えなかった: {'、'.join(found)}")
        return 1
    if dry_run:
        print("（--dry-run なので、書き換えていない）")
        return 0
    table.write_text(csv_text, encoding="utf-8")
    path.with_name(path.name + ".bak").write_text(text, encoding="utf-8")
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    print(f"✅ {table} を作り、{path} から modules・[channels]・[agents] を消した（前のものは {path.name}.bak）")
    print("\nこのあとやること:\n  - Kei Agent を起動し直す（deploy/restart-all.sh）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent agents", description="担当の表（agents.csv）")
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("init", help="config.toml の modules・[channels]・[agents] を agents.csv に移す")
    sub.add_argument("--dry-run", action="store_true", help="作る表を見るだけ（書き換えない）")
    args = parser.parse_args(argv)
    return init(dry_run=args.dry_run)
