"""担当の表（`kei-agent agents init`）。config.toml の modules・[channels]・[agents] を agents.csv に移す。

- 今の設定と同じ中身の agents.csv を作り（AI は App Home で今選んでいるものを engine に書く）、config.toml からその3つを消す（ほかの行とコメントは残す。
  前の設定は config.toml.bak に残す）。書く前に、新しい設定を読めて中身が変わらないことを確かめる
- `--dry-run` で、作る表を見るだけ
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from dataclasses import replace

from kei_agent import agents_table
from kei_agent.config import Config, ConfigError, config_home, config_path, load_config

_TABLE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]")
_MODULES = re.compile(r"^\s*modules\s*=")


def without_replaced(text: str, *, drop_notes: bool = False) -> str:
    """config.toml から modules の行と、[channels]・[agents]・[agents.<名前>] の表を消した中身。

    drop_notes なら、消すもののすぐ上のコメントも消す（例の設定から作るとき。説明だけが残らないように）。
    """
    out: list[str] = []

    def drop_notes_above() -> None:
        while drop_notes and out and out[-1].lstrip().startswith("#"):
            out.pop()

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
                drop_notes_above()
        if skipping:
            i += 1
            continue
        if _MODULES.match(row) and not any(_TABLE.match(r) for r in lines[:i]):
            drop_notes_above()
            # 複数の行にまたがる配列は、閉じ括弧の行まで消す
            while "]" not in lines[i].split("#", 1)[0] and i + 1 < len(lines):
                i += 1
            i += 1
            continue
        out.append(row)
        i += 1
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text) if drop_notes else text


def _same(before: Config, after: Config, providers: dict[str, str]) -> bool:
    """表に移す前と後で、モジュール・チャンネル・AI（App Home で選んだものを含む）が同じか。"""
    expected = {actor: replace(profile, provider=providers.get(actor) or profile.provider)
                for actor, profile in before.agent_profiles.items()}
    return replace(before, agents_table=None, agent_profiles=expected) == replace(after, agents_table=None)


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
        before = load_config(path=path, env={**env, "KEI_AGENT_HOME": str(home)})
        data = tomllib.loads(text)
    except (ConfigError, tomllib.TOMLDecodeError) as e:
        print(f"❌ 今の設定を読めない: {e}")
        return 1
    # App Home で選んだ AI（SQLite）。表に移したあとは起動のときに消えるので、表に書き写す
    from kei_agent.doctor import chosen_providers

    providers = chosen_providers(before)
    csv_text = agents_table.from_config(data, providers)
    new_text = without_replaced(text)
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
        # 確かめるために読んだ設定で固定したモデルを、今の設定に戻す
        from kei_agent.model_policy import pin_models

        pin_models(before.agent_profiles)
    if not _same(before, after, providers):
        print(f"❌ 移すと中身が変わってしまうので、書き換えなかった（{path.name} の modules・[channels]・[agents] を見てください）")
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
