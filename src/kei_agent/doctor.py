"""Kei Agent の点検（`kei-agent doctor`）。今の設定と動きを見て、困っているところと直し方を並べる。

どれも読むだけで、何も書き換えない。秘密情報は、あるかどうかだけを見て、中身は出さない。
問題（❌）があれば終了コード 1、注意（⚠️）だけなら 0。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kei_agent import deploy_check, home, modules, version
from kei_agent.config import Config, ConfigError, load_config

OK, WARN, ERROR = "ok", "warn", "error"
MARKS = {OK: "✅", WARN: "⚠️", ERROR: "❌"}
# 共通の秘密情報のファイル（deploy/_common.sh の require_secrets と同じ）。要る鍵は、本体の modules.CORE_SECRETS と
# オンのモジュールの module.toml の [secrets]
SECRETS_FILE = "kei-agent.zsh"
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$")
# 最近のログの ERROR を数える時間（秒）
RECENT_LOG_SECONDS = 3600
LOG_FILE = Path.home() / "Library" / "Logs" / "kei-agent" / "kei-agent.log"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


@dataclass(frozen=True)
class Finding:
    level: str
    group: str
    text: str
    # 直し方（あれば）
    hint: str = ""


# 設定

def check_config(env: dict[str, str] | None = None) -> tuple[Config | None, list[Finding]]:
    try:
        config = load_config(env=env)
    except ConfigError as e:
        return None, [Finding(ERROR, "設定", f"設定を読めない: {e}", "config.toml を直してから、もう一度点検する")]
    names = "、".join(config.modules) or "なし"
    return config, [Finding(OK, "設定", f"設定を読めた（モジュール {len(config.modules)}: {names}）")]


# 秘密情報

def assigned(path: Path) -> dict[str, bool]:
    """zsh のファイルで決めている変数の名前 → 値が入っているか（中身は返さない）。"""
    found: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _ASSIGN.match(line)
        if match:
            value = match.group(2).strip().strip("'\"")
            # 例の書き方（"xoxb-..."）のままのものは、入っていないとみなす
            found[match.group(1)] = bool(value) and not value.endswith("...")
    return found


def own_secrets_file(directory: Path, module: str) -> Path:
    """そのモジュールのプロセスだけの秘密情報のファイル（共通のもののあとに読む。deploy/run-agent.sh）。"""
    return directory / f"kei-agent-{module}.zsh"


def check_secrets(config: Config) -> list[Finding]:
    directory = config.secrets_dir or (config.user_dir or Path.home() / ".config" / "kei-agent") / "secrets"
    path = directory / SECRETS_FILE
    if not path.is_file():
        return [Finding(ERROR, "秘密情報", f"共通の秘密情報のファイルが無い: {path}",
                        "kei-agent setup で作るか、deploy/README.md の「秘密情報」を見て作る")]
    findings = []
    for file in sorted(directory.glob("kei-agent*.zsh")):
        mode = stat.S_IMODE(file.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            findings.append(Finding(WARN, "秘密情報", f"{file.name} をほかの人も読める（{oct(mode)}）", f"chmod 600 {file}"))
    common = assigned(path)

    def present(owner: modules.ModuleSpec | None, name: str) -> bool:
        # 担当プロセスを持つモジュールの鍵は、そのプロセスだけのファイルにあってもよい
        own = own_secrets_file(directory, owner.name) if owner is not None and owner.port is not None else None
        return common.get(name, False) or (own is not None and own.is_file() and assigned(own).get(name, False))

    wanted = modules.secrets(config.modules)
    missing = [f"{secret.name}（{own_secrets_file(directory, owner.name).name}）" if owner and secret.own_file
               else secret.name for owner, secret in wanted if secret.required and not present(owner, secret.name)]
    if missing:
        findings.append(Finding(ERROR, "秘密情報", f"要る鍵が無い: {'、'.join(missing)}",
                                f"{SECRETS_FILE}（括弧の付いたものは、そのファイル）に書き足す。値はここに出さない"))
    else:
        findings.append(Finding(OK, "秘密情報", "要る鍵がそろっている"))
    groups: dict[str, list[tuple[str, bool]]] = {}
    for owner, secret in wanted:
        if secret.group:
            groups.setdefault(secret.group, []).append((secret.name, present(owner, secret.name)))
    for group, names in groups.items():
        found = [name for name, ok in names if ok]
        if found and len(found) < len(names):
            findings.append(Finding(WARN, "秘密情報", f"{group} の鍵が一部だけ: {'、'.join(found)}"
                                    f"（{'、'.join(name for name, ok in names if not ok)} も入れないと使わない）"))
        elif not found:
            findings.append(Finding(OK, "秘密情報", f"{group} の鍵は入れていない（任意）"))
    return findings


# AI

def chosen_providers(config: Config) -> dict[str, str]:
    """実行役ごとの provider（App Home で選んだもの。無ければ config.toml の [agents]）。状態は読むだけで開く。"""
    picked: dict[str, str] = {}
    if config.db_path.exists():
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
        try:
            for key, value in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'agent.%.provider'"):
                picked[key.split(".")[1]] = value
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    found = {}
    for actor in home.agent_labels(config):
        profile = config.agent_profiles.get(actor)
        found[actor] = picked.get(actor) or (profile.provider if profile is not None else "")
    return found


def check_ai(config: Config, which: Callable[[str], str | None] = shutil.which) -> list[Finding]:
    labels = home.agent_labels(config)
    providers = chosen_providers(config)
    findings = []
    unset = [labels[actor] for actor, provider in providers.items() if not provider]
    if unset:
        findings.append(Finding(ERROR, "AI", f"AI が選ばれていない担当: {'、'.join(unset)}",
                                "Slack の App Home で、担当ごとに Claude か Codex を選ぶ"))
    for provider in sorted({p for p in providers.values() if p}):
        command = {"claude": "claude", "codex": "codex"}[provider]
        where = which(command)
        users = "、".join(labels[a] for a, p in providers.items() if p == provider)
        if where:
            findings.append(Finding(OK, "AI", f"{command} がある（使う担当: {users}）"))
        else:
            findings.append(Finding(ERROR, "AI", f"{command} のコマンドが見つからない（使う担当: {users}）",
                                    f"{command} を入れて、ログインしておく"))
    return findings


# 常駐

def launchctl_state(label: str) -> str:
    """launchd に登録したプロセスの状態（running など。登録されていなければ空）。"""
    try:
        proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    match = re.search(r"^\s*state = (\S+)", proc.stdout, re.MULTILINE)
    return match.group(1) if match else "unknown"


def processes(config: Config) -> dict[str, bool]:
    """Kei Agent のプロセスの名前（launchd の com.kei-agent.<名前>）→ いま使うか。本体と、常駐を持つモジュール。"""
    found = {"assistant": True}
    for name, spec in modules.known().items():
        if spec.port is not None:
            found[name] = name in config.modules
    return found


def check_launchd(config: Config, agents_dir: Path = LAUNCH_AGENTS,
                  state: Callable[[str], str] = launchctl_state) -> list[Finding]:
    findings = []
    for name, wanted in processes(config).items():
        label = f"com.kei-agent.{name}"
        installed = (agents_dir / f"{label}.plist").exists()
        shown = "本体" if name == "assistant" else modules.known()[name].label
        how = "deploy/install.sh" if name == "assistant" else f"deploy/install.sh {name}"
        if wanted and not installed:
            findings.append(Finding(ERROR, "常駐", f"{shown}（{label}）が登録されていない", how))
        elif wanted:
            running = state(label)
            if running == "running":
                findings.append(Finding(OK, "常駐", f"{shown}（{label}）が動いている"))
            else:
                log = "launchd.log" if name == "assistant" else f"{name}-launchd.log"
                findings.append(Finding(ERROR, "常駐", f"{shown}（{label}）が動いていない（{running or '読めない'}）",
                                        f"~/Library/Logs/kei-agent/{log} を見る"))
        elif installed:
            findings.append(Finding(WARN, "常駐", f"オフのモジュールの常駐が残っている: {shown}（{label}）",
                                    f"{how} remove"))
    return findings


async def check_versions(config: Config, fetch=None, disk: str | None = None) -> list[Finding]:
    """動いているプロセスが、リポジトリの今の版か（担当と本体は名刺、ゲートウェイは /health）。"""
    import aiohttp

    disk = disk or version.on_disk()
    findings = []
    known = modules.known()
    async with aiohttp.ClientSession() as http:
        for target, url in deploy_check.targets(config).items():
            name = known[target].label if target in known else target
            found = await (fetch(url) if fetch is not None else deploy_check.running_version(http, url))
            if not found:
                findings.append(Finding(ERROR, "版", f"{name}が答えない（{url}）",
                                        "常駐が動いているか確かめる（deploy/restart-all.sh）"))
            elif version.differs(found, disk):
                findings.append(Finding(WARN, "版", f"{name}が古い版のまま（動いている {found}、手元 {disk}）",
                                        "deploy/restart-all.sh で起動し直す"))
            else:
                findings.append(Finding(OK, "版", f"{name}は今の版（{found}）"))
    return findings


# Notion・道具・ログ

def check_notion(config: Config) -> list[Finding]:
    if "notion" not in config.modules:
        return [Finding(WARN, "Notion", "Notion のモジュールがオフ（研究ホーム・授業ホーム・共通ホームには残さない）")]
    notion = config.notion
    findings = []
    for key, value, lost in (("hub_home", notion.hub_home, "Daily・振り返り・時間記録・予定カレンダー"),
                             ("research_home", notion.research_home, "研究ホーム（Task・ノート・先行研究）"),
                             ("course_home", notion.course_home, "授業ホーム（授業・課題）")):
        if value:
            findings.append(Finding(OK, "Notion", f"[notion] {key} が書いてある"))
        else:
            findings.append(Finding(WARN, "Notion", f"[notion] {key} が空（{lost}を Notion に残さない）"))
    return findings


def check_tools(config: Config, which: Callable[[str], str | None] = shutil.which) -> list[Finding]:
    findings = []
    for module, command, why in (("research", "pueue", "研究のジョブ"), ("improve", "gh", "要望の GitHub issue"),
                                 ("voice", "ffmpeg", "声")):
        if module not in config.modules:
            continue
        if which(command):
            findings.append(Finding(OK, "道具", f"{command} がある（{why}）"))
        else:
            findings.append(Finding(WARN, "道具", f"{command} が見つからない（使うもの: {why}）", f"brew install {command}"))
    return findings


def check_logs(path: Path = LOG_FILE, now: float | None = None) -> list[Finding]:
    """最近1時間のログの ERROR の数（中身は出さない。ログを見るよう伝える）。"""
    if not path.is_file():
        return []
    now = time.time() if now is None else now
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - RECENT_LOG_SECONDS))
    count = 0
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if line[:19] >= since and " ERROR " in line[:60]:
                count += 1
    if count:
        return [Finding(WARN, "ログ", f"最近1時間の ERROR が {count} 件", f"{path} を見る")]
    return [Finding(OK, "ログ", "最近1時間の ERROR は無い")]


# まとめて

async def run(env: dict[str, str] | None = None) -> list[Finding]:
    config, findings = check_config(env)
    if config is None:
        return findings
    findings += check_secrets(config)
    findings += check_ai(config)
    findings += check_launchd(config)
    findings += await check_versions(config)
    findings += check_notion(config)
    findings += check_tools(config)
    findings += check_logs()
    return findings


def report(findings: list[Finding], verbose: bool = False) -> str:
    """点検の結果の文。うまくいっているもの（✅）は、verbose のときだけ並べる。"""
    lines = ["Kei Agent の点検", ""]
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        if verbose or finding.level != OK:
            groups.setdefault(finding.group, []).append(finding)
    for group, found in groups.items():
        lines.append(group)
        for finding in found:
            lines.append(f"  {MARKS[finding.level]} {finding.text}")
            if finding.hint:
                lines.append(f"      → {finding.hint}")
    errors = sum(f.level == ERROR for f in findings)
    warns = sum(f.level == WARN for f in findings)
    oks = sum(f.level == OK for f in findings)
    if not groups:
        lines.append("困っているところは見つからなかった")
    lines += ["", f"まとめ: 問題 {errors} 件、注意 {warns} 件、うまくいっている {oks} 件"
                  + ("" if verbose else "（全部を見るなら --all）")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent doctor", description="今の設定と動きを点検する（読むだけ）")
    parser.add_argument("--all", action="store_true", help="うまくいっているものも並べる")
    args = parser.parse_args(argv)
    findings = asyncio.run(run())
    print(report(findings, verbose=args.all))
    return 1 if any(f.level == ERROR for f in findings) else 0
