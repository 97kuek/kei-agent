"""はじめの設定（`kei-agent setup`。対話）。

質問に答えると、利用者のフォルダ（~/.config/kei-agent/。KEI_AGENT_HOME で変えられる）に、プロフィール（profile.md）・
設定（config.toml）・秘密情報（secrets/kei-agent.zsh と、プロセスだけのもの kei-agent-<名前>.zsh）を作る。

1. 話し方とあなたのこと → profile.md
2. 使うモジュール（できることを並べて選ぶ）と AI（claude / codex）→ agents.csv。
   研究テーマの置き場所、Notion のホームのページ → config.toml
3. AI（claude / codex があるか）
4. Slack App（kei-agent manifest の出力を貼る手順と、作るチャンネル）
5. 秘密情報（画面に出さずに聞き、本人だけが読めるファイルに書く。合言葉は作る。要るものは本体と module.toml の [secrets]）
6. 常駐（聞いてから deploy/install.sh で launchd に登録する）
7. 点検（kei-agent doctor と同じ）と、このあとやること

もうあるファイルは書き換えない（その段は飛ばす。足す・外すは kei-agent module、確かめは kei-agent doctor）。
途中でやめても作ったファイルは残り、もう一度動かすと残りから進む。秘密情報の値は、画面にもログにも出さない。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import secrets
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kei_agent import doctor, module_command
from kei_agent.configuration import agents_table
from kei_agent.configuration.config import (
    EXAMPLE_CONFIG,
    PROFILE_FILE,
    REPO_ROOT,
    Config,
    ConfigError,
    config_home,
    config_path,
    load_config,
    model_actors,
)
from kei_agent.framework import modules

EXAMPLE_PROFILE = REPO_ROOT / "profile.example.md"
DEFAULT_TONE = "一人称は「私」。です・ます調で、短く"
# プロフィールの「依頼者について」に聞くこと（名前と例）
ABOUT = (("所属", "例: ○○大学 ○○学部の学生"), ("研究", "例: 音声の区間検索"), ("興味", "例: AI・LLM・エージェント"))
# 本体の秘密情報の値の頭（取り違えやすいので確かめる）
PREFIXES = {"SLACK_BOT_TOKEN": ("xoxb-",), "SLACK_APP_TOKEN": ("xapp-",), "KEI_AGENT_ALLOWED_USER_ID": ("U", "W")}
# 実行役ができること（module.toml の [actor] の files・notion）の言い方
FILE_WORDS = {"read": "ファイルを読む", "write": "ファイルに書く"}
NOTION_WORDS = {"read": "Notion を読む", "write": "Notion に書く"}
INTRO = """Kei Agent のはじめの設定。質問に答えると、{home} に次のものを作る:
  profile.md（話し方とあなたのこと）、agents.csv（使うモジュールと AI）、config.toml（置き場所など）、secrets/（秘密情報）
もうあるファイルは書き換えない。途中でやめても（Ctrl-C）作ったファイルは残り、もう一度動かすと残りから進む。"""
SECRETS_HEADER = """# Kei Agent の秘密情報（kei-agent setup が作った）。本人だけが読める（chmod 600）まま置き、Git に入れない。
# 書き方は deploy/README.md の「秘密情報」。# で始まる export の行は、まだ入れていないもの（# を外して値を書く）
"""
_ABOUT_LINE = re.compile(r"^- (所属|研究|興味): ")
_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


@dataclass
class Asker:
    """答えを聞く口（試験では差し替える）。secret は画面に出さない入力。"""
    ask: Callable[[str], str] = input
    secret: Callable[[str], str] = getpass.getpass

    def text(self, question: str, default: str = "") -> str:
        answer = self.ask(f"{question}{f'（Enter で {default}）' if default else ''}: ").strip()
        return answer or default

    def yes(self, question: str, default: bool = False) -> bool:
        while True:
            answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "はい"):
                return True
            if answer in ("n", "no", "いいえ"):
                return False
            print("  y か n で答えてください")


def write_new(path: Path, text: str, mode: int = 0o644) -> None:
    """新しいファイルとして書く（もうあれば FileExistsError。書き換えない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# 1. プロフィール

def profile_text(example: str, tone: str, call: str, about: dict[str, str]) -> str:
    """プロフィールの例（profile.example.md）に答えを入れたもの。答えの無い行は外す。"""
    lines = []
    for line in example.splitlines():
        if line.startswith("- 一人称は"):
            lines.append(f"- {tone or DEFAULT_TONE}")
            if call:
                lines.append(f"- 依頼者の呼び方: {call}")
        elif match := _ABOUT_LINE.match(line):
            if about.get(match[1]):
                lines.append(f"- {match[1]}: {about[match[1]]}")
        else:
            lines.append(line)
    text = "\n".join(lines).rstrip() + "\n"
    if not any(about.values()):
        # 依頼者について を1つも答えなければ、見出しも外す
        text = text.replace("\n## 依頼者について\n", "\n").rstrip() + "\n"
    return text


def step_profile(asker: Asker, home: Path) -> None:
    print("\n1. 話し方とあなたのこと（profile.md。会話する担当の指示書に差し込む）")
    path = home / PROFILE_FILE
    if path.exists():
        print(f"  もうある: {path}（書き換えない）")
        return
    tone = asker.text("  Kei Agent の話し方", DEFAULT_TONE)
    call = asker.text("  あなたの呼び方（例: 山田さん。無ければ Enter）")
    about = {name: asker.text(f"  {name}（{example}。無ければ Enter）") for name, example in ABOUT}
    write_new(path, profile_text(EXAMPLE_PROFILE.read_text(encoding="utf-8"), tone, call, about))
    print(f"  ✅ 書いた: {path}（あとから直接書き換えてよい）")


# 2. 設定

def abilities(spec: modules.ModuleSpec) -> str:
    """モジュールの AI の実行役ができることと、要る鍵（オンにする前に見せる）。"""
    parts = []
    actor = spec.actor
    if actor is not None:
        can = [FILE_WORDS.get(actor.files, ""), "コマンド" if actor.shell else "", "Web" if actor.web else "",
               NOTION_WORDS.get(actor.notion, ""),
               f"連携（{'・'.join(c.name for c in actor.connectors)}）" if actor.connectors else ""]
        parts.append("AI: " + ("・".join(c for c in can if c) or "渡した材料だけ"))
    if spec.secrets:
        parts.append("鍵: " + "、".join(s.name + ("" if s.required else "（任意）") for s in spec.secrets))
    return "  ".join(parts)


def choose_modules(asker: Asker, known: dict[str, modules.ModuleSpec]) -> list[str]:
    print("  使えるモジュール（できることを見て、使わないものを外す）:")
    for name, spec in sorted(known.items()):
        mine = "" if spec.builtin else "（あなたのモジュール）"
        print(f"   {name:<10} {spec.label}{mine}  {spec.description}")
        for line in (module_command.describe(spec, None), abilities(spec)):
            if line and line != "—":
                print(f"   {'':<10} {line}")
    while True:
        removed = asker.text("  外すモジュール（名前を空白で区切る。全部使うなら Enter）").replace("、", " ").split()
        unknown = [name for name in removed if name not in known]
        if unknown:
            print(f"  知らないモジュール: {'、'.join(unknown)}")
            continue
        names = sorted(name for name in known if name not in removed)
        lacking = [f"{known[name].label}（{name}）には {'、'.join(r for r in known[name].requires if r not in names)} が要る"
                   for name in names if any(r not in names for r in known[name].requires)]
        if lacking:
            print("  " + "。".join(lacking))
            continue
        return names


def page_id(value: str) -> str:
    """Notion のページの URL か ID から、32文字の ID を取り出す（取り出せなければ空）。"""
    tail = value.strip().split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1].replace("-", "").lower()
    match = re.search(r"[0-9a-f]{32}$", tail)
    return match[0] if match else ""


def ask_page(asker: Asker, question: str) -> str:
    while True:
        answer = asker.text(question)
        if not answer or page_id(answer):
            return page_id(answer)
        print("  ページの URL か、32文字の ID を貼ってください")


def set_value(text: str, table: str, key: str, value: str) -> str:
    """その表（"" なら一番外側）の key の文字の値を value に書き換えた設定（行のあとのコメントは残す）。"""
    lines = text.splitlines(keepends=True)
    row = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*')(.*)$", re.DOTALL)
    current = ""
    for i, line in enumerate(lines):
        if header := _HEADER.match(line):
            current = header[1].strip()
        elif current == table and (match := row.match(line)):
            lines[i] = match[1] + json.dumps(value, ensure_ascii=False) + match[3]
            return "".join(lines)
    raise ConfigError(f"例の設定に {f'[{table}] の ' if table else ''}{key} が無い")


def config_text(example: str, answers: dict[tuple[str, str], str]) -> str:
    """例の設定（config.example.toml）の、答えたところだけを書き換えたもの。"""
    text = example
    for (table, key), value in answers.items():
        text = set_value(text, table, key, value)
    expected = tomllib.loads(example)
    for (table, key), value in answers.items():
        (expected[table] if table else expected)[key] = value
    if tomllib.loads(text) != expected:
        raise ConfigError(f"例の設定（{EXAMPLE_CONFIG.name}）をうまく書き換えられない")
    return text


def choose_engine(asker: Asker, which: Callable[[str], str | None]) -> str:
    """担当の AI。入っているものが1つならそれ、両方なら聞く、無ければ空（あとで agents.csv の engine に書く）。"""
    found = [name for name in modules.PROVIDERS if which(name)]
    if not found:
        print("  ⚠️ claude も codex も見つからないので、AI はまだ選ばない（入れたあと agents.csv の engine 列に書く）")
        return ""
    if len(found) == 1:
        print(f"  AI は {found[0]}（見つかったもの。担当ごとに変えるときは agents.csv の engine 列）")
        return found[0]
    while True:
        engine = asker.text("  担当の AI（claude か codex。担当ごとに変えるときは、あとで agents.csv の engine 列）", "claude")
        if engine in found:
            return engine
        print("  claude か codex で答えてください")


def step_config(asker: Asker, path: Path, home: Path, env: dict[str, str],
                which: Callable[[str], str | None] = shutil.which) -> Config | None:
    print("\n2. 使うモジュールと AI（agents.csv）、置き場所（config.toml）")
    table = home / agents_table.AGENTS_FILE
    table_text = text = None
    if table.exists():
        print(f"  もうある: {table}（書き換えない。足す・外すは kei-agent module add / remove）")
        try:
            names = agents_table.load(table)["modules"]
        except agents_table.TableError as e:
            print(f"  ❌ 表を読めない: {e}（直してから、もう一度 kei-agent setup）")
            return None
    else:
        names = choose_modules(asker, modules.known())
        engine = choose_engine(asker, which)
        where = {"research_root": asker.text("  研究テーマの作業場を置く場所", "~/research")} if "research" in names else {}
        table_text = agents_table.from_config({"modules": names, **where}, {actor: engine for actor in model_actors()})
    if path.exists():
        print(f"  もうある: {path}（書き換えない）")
    else:
        answers: dict[tuple[str, str], str] = {}
        if "notion" in names:
            print("  Notion のホームのページ（URL か ID。使わないものは Enter。あとから config.toml の [notion] に書いてもよい）")
            homes = [("hub_home", "共通ホーム（Daily・振り返り・予定・時間の記録）"),
                     *([("research_home", "研究ホーム")] if "research" in names else []),
                     *([("course_home", "授業ホーム")] if "course" in names else [])]
            for key, label in homes:
                if found := ask_page(asker, f"  {label}"):
                    answers[("notion", key)] = found
        try:
            text = config_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), answers)
        except ConfigError as e:
            print(f"  ❌ この答えでは設定を読めないので、書かなかった: {e}")
            return None
    # 新しく書くものは、どちらもまだ書かずに一緒に確かめる
    trials = {target.with_name(f".{target.name}.trial"): content
              for target, content in ((path, text), (table, table_text)) if content is not None}
    try:
        for trial, content in trials.items():
            trial.write_text(content, encoding="utf-8")
        load_config(path=path.with_name(f".{path.name}.trial") if text is not None else path,
                    env={**env, "KEI_AGENT_HOME": str(home)},
                    agents_csv=table.with_name(f".{table.name}.trial") if table_text is not None else None)
    except ConfigError as e:
        print(f"  ❌ 設定を読めないので、書かなかった: {e}（直してから、もう一度 kei-agent setup）")
        return None
    finally:
        for trial in trials:
            trial.unlink(missing_ok=True)
    if table_text is not None:
        write_new(table, table_text)
        print(f"  ✅ 書いた: {table}（モジュールのオンオフ・チャンネル・AI は、ここを書き換える）")
    if text is not None:
        write_new(path, text)
        print(f"  ✅ 書いた: {path}（時刻や Notion のホームは、ここを直接書き換える）")
    return load_config(env=env)


# 3. AI と 4. Slack

def step_ai(which: Callable[[str], str | None]) -> None:
    print("\n3. AI（Claude Code / Codex）")
    found = [name for name in modules.PROVIDERS if which(name)]
    if found:
        print(f"  ✅ 見つかった: {'、'.join(found)}（ログインしておく）")
    else:
        print("  ⚠️ claude も codex も見つからない。どちらかを入れて、ログインしておく")
    print("  担当ごとの AI は agents.csv の engine 列（空の担当は動かない）。App Home では一時的に切り替えられる")


def channel_lines(config: Config) -> list[str]:
    """作るチャンネルと、その役目（名前は番号を外したもの。「か」でつないだものは、どれか1つでよい）。"""
    def names(values) -> str:
        return " か ".join(f"#{name}" for name in values)

    overview = "研究全体の相談" + ("（Daily と振り返りもここに出る）" if "daily" in config.modules else "")
    lines = [f"{names(config.overview_channels)}: {overview}",
             f"{names(config.improve_channels)}: Kei Agent への要望と、うまく動かなかったときの知らせ"]
    for spec in modules.enabled(config.modules):
        for kind, default in spec.channels.items():
            values = config.module_channels.get(kind, default)
            if values == (modules.ALL_CHANNELS,):
                lines.append(f"ほかの名前のチャンネル: {spec.label}（招いたチャンネルが、それぞれ研究テーマになる）")
            else:
                lines.append(f"{names(values)}: {spec.label}")
    return lines


def step_slack(config: Config) -> None:
    print("\n4. Slack App（手でやること）")
    todo = ["https://api.slack.com/apps → Create New App → From a manifest で、uv run kei-agent manifest の出力を貼る"
            "（uv run kei-agent manifest | pbcopy でクリップボードに写せる）",
            "Install App で入れ、Bot User OAuth Token（xoxb-）を控える",
            "Basic Information → App-Level Tokens で、scope が connections:write のトークン（xapp-）を作って控える",
            "Agents の Agent experience をオンにして入れ直す（入力欄の下の経過表示に使う）",
            "Slack のプロフィールの ︙ から、自分のメンバー ID（U…）を控える",
            "チャンネルを作る（頭に 00_ などの番号を付けてよい。番号は外して照合する）:"]
    for number, line in enumerate(todo, 1):
        print(f"  {number}) {line}")
    for line in channel_lines(config):
        print(f"     - {line}")
    print("  控えたトークンと ID は、次の段で聞く")


# 5. 秘密情報

def ask_secret(asker: Asker, secret: modules.SecretSpec) -> str:
    """画面に出さずに聞く（空なら飛ばす）。合言葉は作る。"""
    if secret.generate:
        print(f"  {secret.name}: 作った（{secret.description}）")
        return secrets.token_hex(32)
    print(f"  {secret.description}")
    note = "" if secret.required else "任意。"
    while True:
        value = asker.secret(f"  {secret.name}（{note}画面には出ない。飛ばすなら Enter）: ").strip()
        prefixes = PREFIXES.get(secret.name)
        if not value:
            return ""
        if "'" in value:
            print("  ' は使えない。もう一度")
        elif prefixes and not value.startswith(prefixes):
            print(f"  ⚠️ {' か '.join(prefixes)} で始まっていない。取り違えていないか確かめて、もう一度")
        else:
            return value


def secrets_text(entries: list[tuple[modules.SecretSpec, str]]) -> str:
    lines = [SECRETS_HEADER]
    for secret, value in entries:
        lines.append(f"# {secret.description}{'' if secret.required else '（任意）'}")
        lines.append(f"export {secret.name}='{value}'" if value else f"# export {secret.name}=''")
    return "\n".join(lines) + "\n"


def step_secrets(asker: Asker, config: Config) -> bool:
    """秘密情報のファイルを作る。要る鍵がそろったか（常駐を登録してよいか）を返す。"""
    print("\n5. 秘密情報（画面には出さない。本人だけが読めるファイルに書く）")
    directory = config.secrets_dir or (config.user_dir or Path.home() / ".config" / "kei-agent") / "secrets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    files: dict[Path, list[modules.SecretSpec]] = {}
    for owner, secret in modules.secrets(config.modules):
        own = owner is not None and secret.own_file
        path = doctor.own_secrets_file(directory, owner.name) if own else directory / doctor.SECRETS_FILE
        files.setdefault(path, []).append(secret)
    for path, wanted in files.items():
        if path.exists():
            print(f"  もうある: {path}（書き換えない）")
            continue
        print(f"  {path.name}:")
        entries = [(secret, ask_secret(asker, secret)) for secret in wanted]
        if path.name != doctor.SECRETS_FILE and not any(value for _, value in entries):
            print(f"  （何も入れなかったので {path.name} は作らない）")
            continue
        write_new(path, secrets_text(entries), mode=0o600)
        print(f"  ✅ 書いた: {path}（本人だけが読める）")
    problems = [f for f in doctor.check_secrets(config) if f.level == doctor.ERROR]
    for problem in problems:
        print(f"  ⚠️ {problem.text}")
    if problems:
        print("  入れていない鍵は、あとでファイルの「# export …」の行に書き足す（頭の # を外し、'' の中に値を書く）")
    return not problems


# 6. 常駐と 7. 点検

def step_launchd(asker: Asker, config: Config, ready: bool, installer: Callable[[str, bool], bool]) -> None:
    print("\n6. 常駐（launchd。ログインしたときに起動し、落ちたら起こし直す）")
    specs = [spec for spec in modules.enabled(config.modules) if spec.port is not None]
    # ほかのプロセスが使う常駐（Notion のゲートウェイ）→ 担当 → 本体の順
    order = [spec for spec in specs if spec.service] + [spec for spec in specs if not spec.service]
    commands = "、".join([*(f"deploy/install.sh {spec.name}" for spec in order), "deploy/install.sh"])
    labels = [*(spec.label for spec in order), "本体"]
    if not ready:
        print(f"  要る鍵がそろっていないので、登録は後にする。そろえたら順に: {commands}")
        return
    if not asker.yes(f"  登録して起動しますか（{'、'.join(labels)}）"):
        print(f"  登録しなかった。するときは順に: {commands}")
        return
    for name, label in zip([*(spec.name for spec in order), ""], labels, strict=True):
        done = installer(name, False)
        print(f"  {'✅' if done else '⚠️'} {label}{'を登録した' if done else 'を登録できなかった'}")


def _doctor(env: dict[str, str]) -> list[doctor.Finding]:
    return asyncio.run(doctor.run(env))


def next_steps(config: Config) -> list[str]:
    steps = ["Slack で、作ったチャンネルに Kei Agent を招く"]
    if any(not p.provider for p in config.agent_profiles.values()):
        steps.append("agents.csv の engine 列に、担当ごとの AI（claude か codex）を書く")
    if "notion" in config.modules:
        steps.append("Notion のホームのページをコネクト「Kei Agent」に共有し、ゲートウェイが動いてから DB を作る"
                     "（deploy/README.md の「5. Notion」）")
    for spec in modules.enabled(config.modules):
        if spec.settings:
            steps.append(f"{spec.label}の設定は config.toml の [{spec.name}] で変えられる: {'、'.join(spec.settings)}"
                         f"（説明は modules/{spec.name}/module.toml）")
    steps.append("困ったら uv run kei-agent doctor。モジュールの足し外しは uv run kei-agent module。詳しくは deploy/README.md")
    return steps


def run(asker: Asker | None = None, env: dict[str, str] | None = None, *,
        which: Callable[[str], str | None] = shutil.which,
        installer: Callable[[str, bool], bool] = module_command.install,
        check: Callable[[dict[str, str]], list[doctor.Finding]] = _doctor) -> int:
    asker = asker or Asker()
    env = dict(os.environ) if env is None else env
    home, path = config_home(env), config_path(env)
    print(INTRO.format(home=home))
    try:
        home.mkdir(parents=True, exist_ok=True)
        modules.register_user_modules(home / "modules")
        step_profile(asker, home)
        config = step_config(asker, path, home, env, which)
        if config is None:
            return 1
        step_ai(which)
        step_slack(config)
        ready = step_secrets(asker, config)
        step_launchd(asker, config, ready, installer)
    except modules.ModuleError as e:
        print(f"❌ モジュールを読めない: {e}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n中断した（作ったファイルはそのまま。もう一度 kei-agent setup を動かすと、残りから進む）")
        return 130
    print("\n7. 点検（kei-agent doctor と同じ）")
    print(doctor.report(check(env)))
    print("（常駐の登録や agents.csv での AI の選択の前は、ここに問題が出る。このあとやることを済ませてから、"
          "もう一度 uv run kei-agent doctor）")
    print("\nこのあとやること:")
    for step in next_steps(config):
        print(f"  - {step}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent setup",
                                     description="はじめの設定（対話）。もうあるファイルは書き換えない")
    parser.parse_args(argv)
    return run()
