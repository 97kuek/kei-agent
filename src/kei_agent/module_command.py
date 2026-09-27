"""モジュールのオン・オフ（`kei-agent module list / add / remove`）。ひな形とテストは `new` / `test`（module_scaffold.py）。

- `list` … 知っているモジュール（組み込みと、利用者のフォルダの modules/）と、オンかどうか、持っているもの
- `add <名前>` / `remove <名前>` … config.toml の `modules` を書き換え（ほかの行とコメントは残す。書く前に、新しい設定を
  読めるか確かめる。前の設定は config.toml.bak に残す）、常駐を持つモジュールなら launchd に登録する・外す。
  そのあとにやること（起動し直す、manifest の貼り直し、チャンネル、設定できる項目）を並べる。`--dry-run` で見るだけ

`modules` を書いていない設定は、組み込みのモジュールを全部使う。そこで足す・外すときは、今の一覧を書き出してから変える。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

from kei_agent import module_scaffold, modules
from kei_agent.config import REPO_ROOT, Config, ConfigError, config_home, config_path, load_config

# 書き足すときに添える行
COMMENT = "# 使うモジュール（kei-agent module add / remove が書き換える。書かなければ組み込みを全部使う）"
_TABLE = re.compile(r"^\s*\[")
_MODULES = re.compile(r"^\s*modules\s*=")


def _parse(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"書き方が TOML として読めません: {e}") from None


def current(text: str) -> list[str] | None:
    """設定の modules（書いていなければ None）。読めない・名前の配列でないときは ConfigError。"""
    data = _parse(text)
    if "modules" not in data:
        return None
    value = data["modules"]
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError('modules は、モジュールの名前の配列にしてください（例: modules = ["knowledge"]）')
    return list(value)


def only_modules_changed(text: str, new_text: str, names: list[str]) -> bool:
    """書き換えた設定で、modules が names になり、ほかの中身は変わっていないか。"""
    try:
        before, after = _parse(text), _parse(new_text)
    except ConfigError:
        return False
    before.pop("modules", None)
    return after.pop("modules", None) == names and after == before


def with_modules(text: str, names: list[str]) -> str:
    """modules の行だけを names に書き換えた設定。無ければ、最初の表（[...]）の前に書き足す。"""
    line = "modules = [" + ", ".join(f'"{name}"' for name in names) + "]"
    lines = text.splitlines(keepends=True)
    for start, row in enumerate(lines):
        if _TABLE.match(row):
            break
        if _MODULES.match(row):
            end = start
            # 複数の行にまたがる配列は、閉じ括弧の行まで置き換える
            while "]" not in lines[end].split("#", 1)[0] and end + 1 < len(lines):
                end += 1
            return "".join([*lines[:start], line + "\n", *lines[end + 1:]])
    first_table = next((i for i, row in enumerate(lines) if _TABLE.match(row)), len(lines))
    head, tail = lines[:first_table], lines[first_table:]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    return "".join([*head, COMMENT + "\n", line + "\n", *(["\n"] if tail else []), *tail])


def check_text(path: Path, text: str, env: dict[str, str], home: Path) -> Config:
    """その中身の設定を読めるか確かめる（同じフォルダの一時ファイルで。自分のモジュールと themes.toml も読む）。
    読めなければ ConfigError。"""
    trial = path.with_name(f".{path.name}.trial")
    trial.write_text(text, encoding="utf-8")
    try:
        return load_config(path=trial, env={**env, "KEI_AGENT_HOME": str(home)})
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
    schedules = [s.label for s in spec.schedules] + [{"daily": "Daily", "review": "振り返り"}[n]
                                                      for n in spec.core_schedules]
    if schedules:
        parts.append("定期処理 " + "・".join(schedules))
    if spec.slash_commands:
        parts.append("コマンド " + "・".join(f"/{c}" for c in spec.slash_commands))
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
    if spec.slash_commands:
        steps.append("Slack App の manifest を貼り直す（kei-agent manifest の出力を App Manifest の画面に貼り、"
                     "Install App で入れ直す）")
    if not added:
        return steps
    channels = [name for kind in spec.channels for name in config.module_channels.get(kind, ())
                if name != modules.ALL_CHANNELS]
    if channels:
        steps.append("チャンネルを作って Kei Agent を招く: " + "、".join(f"#{name}" for name in channels))
    if spec.settings:
        steps.append(f"設定できる項目（config.toml の [{spec.name}]）: " + "、".join(spec.settings))
    if spec.port is not None:
        steps.append(f"そのプロセスだけの秘密情報があれば kei-agent-{spec.name}.zsh に書く（秘密情報の置き場所に。任意）")
    if spec.actor is not None:
        steps.append(f"Slack の App Home で「{spec.label}」の AI（Claude か Codex）を選ぶ")
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
    text = path.read_text(encoding="utf-8")
    try:
        names = current(text)
    except ConfigError as e:
        print(f"❌ 設定を読めない（{path.name}）: {e}")
        return 1
    names = list(modules.builtin()) if names is None else names
    if (name in names) == add:
        print(f"モジュール「{name}」はもう{'オン' if add else 'オフ'}です")
        return 0
    # 足すときは後ろに付ける（名前の順に並んでいれば、その順を保つ）
    new_names = ([*names, name] if names != sorted(names) else sorted([*names, name])) if add else \
        [n for n in names if n != name]
    new_text = with_modules(text, new_names)
    if not only_modules_changed(text, new_text, new_names):
        print(f"❌ modules の行をうまく書き換えられないので、書き換えなかった（{path} の modules を手で直してください）")
        return 1
    try:
        config = check_text(path, new_text, env, home)
    except ConfigError as e:
        print(f"❌ この変更では設定を読めなくなるので、書き換えなかった: {e}")
        return 1
    verb = "足す" if add else "外す"
    print(f"モジュール「{name}」（{spec.label}）を{verb}: modules = {new_names}")
    if dry_run:
        print("（--dry-run なので、書き換えていない）")
        return 0
    path.with_name(path.name + ".bak").write_text(text, encoding="utf-8")
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    print(f"✅ {path} を書き換えた（前のものは {path.name}.bak）")
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
