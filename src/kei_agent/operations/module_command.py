"""モジュールのオン・オフ（`kei-agent module list / add / remove`）。ひな形とテストは `new` / `test`（module_scaffold.py）。

- `list` … 知っているモジュール（組み込みと、利用者のフォルダの modules/）と、オンかどうか、持っているもの
- `add <名前>` / `remove <名前>` … 担当の表（agents.csv）のその行の enabled を書き換える（ほかの行は残す。書く前に、
  新しい設定を読めるか確かめる。前の表は agents.csv.bak に残す。表が無ければ、組み込み全部がオンの表から作る）、常駐を持つモジュールなら launchd に登録する・外す。
  そのあとにやること（起動し直す、MCP の設定、チャンネル、設定できる項目）を並べる。`--dry-run` で見るだけ
"""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from kei_agent.configuration import agents_table
from kei_agent.configuration.config import REPO_ROOT, Config, ConfigError, config_home, config_path, load_config
from kei_agent.framework import modules
from kei_agent.operations import module_scaffold
from kei_agent.storage.settings import CORE_SCHEDULES


def check_text(path: Path, text: str, env: dict[str, str], home: Path, *, table: Path | None = None) -> Config:
    """その中身の設定を読めるか確かめる（同じフォルダの一時ファイルで。自分のモジュールと themes.toml も読む）。
    table を渡すと、text は担当の表（agents.csv）の中身（table はまだ無くてよい）。読めなければ ConfigError。"""
    target = table or path
    trial = target.with_name(f".{target.name}.trial")
    trial.write_text(text, encoding="utf-8")
    env = {**env, "KEI_AGENT_HOME": str(home)}
    try:
        if table is not None:
            return load_config(path=path, env=env, agents_csv=trial)
        return load_config(path=trial, env=env)
    finally:
        trial.unlink(missing_ok=True)


def install(name: str, remove: bool = False) -> bool:
    """deploy/install.sh で、そのモジュールの常駐を launchd に登録する（remove なら外す）。名前が空なら本体。"""
    args = [str(REPO_ROOT / "deploy" / "install.sh"), *([name] if name else []), *(["remove"] if remove else [])]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"⚠️ 常駐の登録を変えられませんでした: {e}")
        return False
    if proc.returncode != 0:
        print(f"⚠️ 常駐の登録を変えられませんでした: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.returncode == 0


def describe(spec: modules.ModuleSpec, config: Config | None) -> str:
    parts = []
    if spec.port is not None:
        parts.append(f"常駐 {spec.port}")
    kinds = spec.channels
    names = [name for kind in kinds for name in (config.module_channels.get(kind) if config and kind in
                                                  config.module_channels else kinds[kind])]
    if names:
        parts.append("チャンネル " + "・".join("ほかのどれにも当たらないもの" if n == modules.ALL_CHANNELS else f"#{n}"
                                          for n in names))
    if spec.core_channels:
        parts.append("Kei Agent のチャンネル")
    schedules = [s.label for s in spec.schedules] + [CORE_SCHEDULES[n] for n in spec.core_schedules]
    if schedules:
        parts.append("定期処理 " + "・".join(schedules))
    return "、".join(parts) or "—"


def list_modules(env: dict[str, str] | None = None) -> int:
    try:
        config = load_config(env=env)
    except ConfigError as e:
        print(f"❌ 設定を読めない: {e}")
        return 1
    known = modules.known()
    print(f"モジュール（オン {len(config.modules)} / 知っている {len(known)}）")
    for name, spec in sorted(known.items()):
        mark = "✅" if name in config.modules else "・"
        mine = "" if spec.builtin else "（あなたのモジュール）"
        print(f"  {mark} {name:<10} {spec.label}{mine}  {describe(spec, config)}")
    return 0


def next_steps(spec: modules.ModuleSpec, config: Config, added: bool) -> list[str]:
    """オン・オフを変えたあとに、利用者がやること。"""
    steps = ["Kei Agent を起動し直す（deploy/restart-all.sh）"]
    if not added:
        return steps
    channels = [name for kind in spec.channels for name in config.module_channels.get(kind, ())
                if name != modules.ALL_CHANNELS]
    if channels:
        shown = [f"#{modules.channel_prefix(name)}<名前>" if modules.channel_prefix(name) else f"#{name}" for name in channels]
        steps.append("チャンネルを作って Dot を招く: " + "、".join(shown))
    if spec.settings:
        steps.append(f"設定できる項目（config.toml の [{spec.name}]）: " + "、".join(spec.settings))
    if spec.port is not None:
        steps.append(f"そのプロセスだけの秘密情報があれば kei-agent-{spec.name}.zsh に書く（秘密情報の置き場所に。任意）")
    if spec.actor is not None and not config.agent_profiles[spec.name].provider:
        steps.append(f"agents.csv の {spec.name} の行の engine に claude か codex を書く")
    return steps


def change(name: str, add: bool, *, env: dict[str, str] | None = None, dry_run: bool = False,
           launchd: bool = True, installer: Callable[[str, bool], bool] = install) -> int:
    """modules に足す（add）・外す。書く前に、新しい設定を読めるか確かめる。"""
    env = dict(os.environ) if env is None else env
    path = config_path(env)
    if not path.is_file():
        print(f"❌ 設定ファイルが無い: {path}（先に kei-agent setup か、config.example.toml を写す）")
        return 1
    home = config_home(env)
    try:
        modules.register_user_modules(home / "modules")
    except modules.ModuleError as e:
        print(f"❌ モジュールを読めない: {e}")
        return 1
    spec = modules.known().get(name)
    if spec is None:
        print(f"❌ 知らないモジュール: {name}（kei-agent module list で見る）")
        return 1
    table = home / agents_table.AGENTS_FILE
    existed = table.is_file()
    try:
        # 表が無ければ、今の動き（組み込み全部がオン、AI は未選択）と同じ表から始める
        text = agents_table.read_text(table) if existed else agents_table.build()
        names = agents_table.parse(text)["modules"]
    except agents_table.TableError as e:
        print(f"❌ 設定を読めない（{table.name}）: {e}")
        return 1
    if (name in names) == add:
        print(f"モジュール「{name}」はもう{'オン' if add else 'オフ'}です")
        return 0
    new_text = agents_table.with_enabled(text, name, add)
    try:
        config = check_text(path, new_text, env, home, table=table)
    except ConfigError as e:
        print(f"❌ この変更では設定を読めなくなるので、書き換えなかった: {e}")
        return 1
    verb = "足す" if add else "外す"
    print(f"モジュール「{name}」（{spec.label}）を{verb}: {table.name} の {name} の行を enabled = {'true' if add else 'false'}")
    if dry_run:
        print("（--dry-run なので、書き換えていない）")
        return 0
    if existed:
        table.with_name(table.name + ".bak").write_text(text, encoding="utf-8")
    tmp = table.with_name(f".{table.name}.new")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, table)
    print(f"✅ {table} を{'書き換えた（前のものは ' + table.name + '.bak）' if existed else '作った'}")
    steps = next_steps(spec, config, add)
    if spec.port is not None:
        if launchd and installer(name, not add):
            print(f"✅ 常駐（com.kei-agent.{name}）を{'登録した' if add else '外した'}")
        else:
            # --no-launchd のときと、登録を変えられなかったとき
            steps.insert(0, f"常駐の登録を{'する' if add else '外す'}: deploy/install.sh {name}{'' if add else ' remove'}")
    print("\nこのあとやること:")
    for step in steps:
        print(f"  - {step}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent module",
                                     description="モジュールを一覧にする・足す・外す・作る・テストする")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="知っているモジュールと、オンかどうか")
    for command, text in (("add", "モジュールを足す"), ("remove", "モジュールを外す")):
        sub = commands.add_parser(command, help=text)
        sub.add_argument("name", help="モジュールの名前")
        sub.add_argument("--dry-run", action="store_true", help="何が変わるかだけを見る（書き換えない）")
        sub.add_argument("--no-launchd", action="store_true", help="常駐の登録は変えない")
    new = commands.add_parser("new", help="モジュールのひな形を作る（設定は変えない。オンにするのは add）")
    new.add_argument("name", help="モジュールの名前（英小文字・数字・-）")
    new.add_argument("--ai", action="store_true", help="AI の実行役（指示書と用途）を足す")
    new.add_argument("--process", action="store_true", help="担当プロセス（agent.py と番地）を足す")
    new.add_argument("--builtin", action="store_true", help="リポジトリの modules/ に作る（Kei Agent に足して公開するとき）")
    new.add_argument("--label", default="", help="表示名（無ければ名前）")
    new.add_argument("--description", default="", help="何をするモジュールか")
    test = commands.add_parser("test", help="モジュールの tests/ を、本物に触れない柵を付けて pytest で動かす")
    test.add_argument("name", help="モジュールの名前")
    args, rest = parser.parse_known_args(argv)
    if args.command == "test":
        # 残りの引数（-k など）は pytest に渡す
        return module_scaffold.run_tests(args.name, rest)
    if rest:
        parser.error(f"知らない引数です: {' '.join(rest)}")
    if args.command == "list":
        return list_modules()
    if args.command == "new":
        return module_scaffold.create(args.name, ai=args.ai, process=args.process, builtin=args.builtin,
                                      label=args.label, description=args.description)
    return change(args.name, args.command == "add", dry_run=args.dry_run, launchd=not args.no_launchd)
