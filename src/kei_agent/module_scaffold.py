"""モジュールのひな形を作る（`kei-agent module new`）と、モジュールのテストを動かす（`kei-agent module test`）。

- new <名前> … 利用者のフォルダの modules/<名前>/ に、定義（module.toml）・本体側の Python（module.py）・テストを作る。
  --ai で AI の実行役（指示書と用途）、--process で担当プロセス（agent.py と番地）を足す。--builtin ならリポジトリの
  modules/ に作る（Kei Agent に足して公開する人向け）。設定（config.toml）は変えない（オンにするのは module add）
- test <名前> … そのモジュールの tests/ を、本物に触れない柵（kei_agent.testing.plugin）を付けて pytest で動かす
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from string import Template

from kei_agent import modules
from kei_agent.configuration.config import config_home

# 利用者のモジュールの担当プロセスの番地は、ここから空いているものを使う（組み込みは 8786〜8792）
FIRST_PORT = 8800

MODULE_TOML = Template('''# ${label}のモジュール（kei-agent module new が作った。書き方は docs/modules.md の「module.toml」）
api = 1
name = "${name}"
label = "${label}"
description = "${description}"
${actor}${process}
# チャンネルの種類 = 既定の名前（番号を外した名前。agents.csv の channels 列で変えられる）
[channels]
${name} = ["${name}"]

# ほかに書けるもの（使うときにコメントを外す）
# 頼るモジュール（requires は必須、optional はあれば使う）
# [depends]
# requires = ["notion"]
# 定期処理（時刻は設定の [schedule] と App Home で変えられる）
# [schedules.${ident}_daily]
# label = "${label}の見回り"
# default = "07:30"
# Slack のスラッシュコマンド（kei-agent manifest が Slack App に載せる）
# [slash_commands]
# ${name} = "${label}に頼む"
# 設定（config.toml の [${name}] で変えられる）
# [settings]
# place = "東京"
# 要る秘密情報（kei-agent setup が聞き、kei-agent doctor が確かめる。値は書かない）
# [secrets]
# ${secret} = { description = "${label}の API のキー", required = true }
''')

ACTOR = Template('''
# AI の実行役（provider は agents.csv の engine）。どこまで触れるかは、ここに書いたものが制限の表の行になる
[actor]
prompt = "${name}.md"
files = "none"                  # none / read / write（作業場のファイル）
shell = false                   # コマンドを使うか
web = false                     # Web を使うか
notion = "none"                 # none / read / write（Notion ゲートウェイ）
default_use_case = "${use_case}"

# 用途ごとのモデル（コアのモデルの一覧の中からだけ選べる）
[use_cases.${use_case}]
claude = { model = "claude-sonnet-5", effort = "medium" }
codex = { model = "gpt-6-luna", effort = "medium" }
''')

PROCESS = Template('''
# 担当プロセス（A2A の担当。127.0.0.1 のこの番地で動く。手元なら kei-agent-module ${name}、常駐は deploy/install.sh ${name}）
[process]
port = ${port}
''')

MODULE_PY = Template('''"""${label}のモジュール（本体側）。kei-agent module new が作った。書き方は docs/modules.md の「module.py」。

コアとのやり取りは窓口 core（kei_agent.api.Core）だけを通す。
"""

${imports}


class Module:
    def __init__(self, core: Core):
        self.core = core

    def welcome(self) -> str:
        """チャンネルに招かれたときの案内。"""
        return "${label}のチャンネルです。ここで頼まれたことに答えます。"

    async def on_message(self, req: Request, skill: str = "", params: dict | None = None) -> None:
        """このモジュールのチャンネルで頼まれた。"""
${body}''')

BODIES = {
    (False, False): '''        await self.core.reply(req, f"受け取りました: {req.text}")
''',
    (True, False): '''        # AI を1回動かして答える（用途は module.toml の [use_cases]）。req を渡すと、経過と答えをスレッドに出す
        await self.core.run_ai("${use_case}", req.text, req=req)
''',
    (False, True): '''        # 担当プロセス（agent.py）に仕事を頼む
        reply = await self.core.ask_agent("hello", {"text": req.text})
        await self.core.reply(req, reply.text if reply.ok else "担当が答えなかった", failed=not reply.ok)
''',
    (True, True): '''        # スレッドの会話として、担当プロセスの AI に聞いて答える（会話の続き・経過・上限は本体が受け持つ）
        await self.core.converse(req)
''',
}

AGENT_PY = Template('''"""${label}の担当プロセス。kei-agent module new が作った。書き方は docs/modules.md の「agent.py」。

読み込んでよい Kei Agent の部品は、窓口の kei_agent_a2a.api だけ。
"""

import json

from kei_agent_a2a.api import ${imports}

# 名刺に載せる仕事（本体の振り分け係が読む）
SKILLS = [
    AgentSkill(id="hello", name="あいさつ", description="本文の JSON（text）に、あいさつを返す", tags=["${name}"]),
${ask_skill}]


class Executor(SkillExecutor):
    async def handle(self, updater, metadata, text):
        skill = metadata.get("skill")
${ask_branch}        if skill != "hello":
            await self.fail(updater, f"知らない仕事です: {skill}")
            return
        await self.done(updater, "こんにちは。受け取りました: " + str(json.loads(text).get("text", "")))
''')

PROMPT_MD = Template('''# ${label}

あなたは Kei Agent の${label}の担当。依頼者の頼みに答える（何を受け持つかを、ここに書く）。

## 返答

- `<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間だけが Slack に出る。marker の外には何も書かない
- 作業手順、tool / skill / CLI / provider 名、手元の絶対パス、環境エラーは Slack に出さない
- 話し方（一人称・口調）は、最後の「依頼者のプロフィール」の「話し方」に従う（無ければ、です・ます調）で、短く
- 分からないことは、分からないと言う
''')

TEST_PY = Template('''"""${label}のモジュールのテスト（kei-agent module test ${name} で動かす）。

module_kit が、このモジュールを偽物の Slack・AI・担当と一緒に本体の中で動かす（docs/modules.md の
「テストの書き方」）。本物の秘密情報・状態・launchd には触れない。
"""

from pathlib import Path
${pytest_import}
MODULE = Path(__file__).resolve().parents[1]


async def test_it_introduces_itself_when_invited(module_kit):
    kit = module_kit(MODULE)
    await kit.invite()
    assert kit.texts()[-1].endswith("${label}のチャンネルです。ここで頼まれたことに答えます。")


async def test_it_answers_in_its_channel(module_kit):
${answer_test}''')

ANSWER_TESTS = {
    (False, False): '''    kit = module_kit(MODULE)
    ts = await kit.message("こんにちは")
    assert kit.thread(ts) == ["受け取りました: こんにちは"]
''',
    (True, False): '''    kit = module_kit(MODULE)
    kit.ai.answer("答えです")                  # AI が次に返す答え
    ts = await kit.message("教えて")
    assert kit.thread(ts) == ["答えです"]
    assert kit.ai.calls[0]["use_case"] == "${use_case}"
''',
    (False, True): '''    pytest.importorskip("a2a", reason="担当プロセスのテストは uv run --group agents で動かす")
    kit = module_kit(MODULE)
    ts = await kit.message("やあ")
    assert kit.thread(ts) == ["こんにちは。受け取りました: やあ"]
    reply = await kit.skill("hello", {"text": "おーい"})   # 担当の仕事を直接頼む
    assert reply.ok and reply.text == "こんにちは。受け取りました: おーい"
''',
    (True, True): '''    pytest.importorskip("a2a", reason="担当プロセスのテストは uv run --group agents で動かす")
    kit = module_kit(MODULE)
    kit.ai.answer("答えです")                  # 担当プロセスの AI が次に返す答え
    ts = await kit.message("教えて")
    assert kit.thread(ts) == ["答えです"]
    reply = await kit.skill("hello", {"text": "おーい"})   # 担当の仕事を直接頼む
    assert reply.ok and reply.text == "こんにちは。受け取りました: おーい"
''',
}


def free_port(used: set[int]) -> int:
    """担当プロセスの番地（知っているモジュールと重ならず、いま手元で使われていないもの）。"""
    port = FIRST_PORT
    while True:
        if port not in used:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    pass
        port += 1


def files_for(name: str, *, ai: bool, process: bool, label: str = "", description: str = "",
              port: int = 0) -> dict[str, str]:
    """ひな形のファイル（モジュールのフォルダからの場所 → 中身）。"""
    ident = name.replace("-", "_")
    label = label or name
    values = {"name": name, "ident": ident, "label": label, "use_case": f"{ident}_answer", "port": str(port),
              "description": description or f"{label}のモジュール（何をするかを書く）",
              "secret": f"{ident.upper()}_API_KEY"}
    values["actor"] = ACTOR.substitute(values) if ai else ""
    values["process"] = PROCESS.substitute(values) if process else ""
    imports = ["from kei_agent.api import Core, Request"]
    body = Template(BODIES[(ai, process)]).substitute(values)
    found = {
        "module.toml": MODULE_TOML.substitute(values),
        "module.py": MODULE_PY.substitute(values, imports="\n".join(imports), body=body),
        f"tests/test_{ident}.py": TEST_PY.substitute(
            values, pytest_import="\nimport pytest\n" if process else "",
            answer_test=Template(ANSWER_TESTS[(ai, process)]).substitute(values)),
    }
    if ai:
        found[f"{name}.md"] = PROMPT_MD.substitute(values)
    if process:
        ask_skill = ('    AgentSkill(id=ASK, name="質問に答える", description="選択済み provider が答える", '
                     f'tags=["{name}"]),\n') if ai else ""
        ask_branch = ("        if skill == ASK:\n            await self.answer(updater, text)   # 自由な質問は、ほかの担当と同じ形で\n"
                      "            return\n") if ai else ""
        found["agent.py"] = AGENT_PY.substitute(values, imports="ASK, AgentSkill, SkillExecutor" if ai
                                                else "AgentSkill, SkillExecutor", ask_skill=ask_skill,
                                                ask_branch=ask_branch)
    return found


def create(name: str, *, ai: bool = False, process: bool = False, builtin: bool = False, label: str = "",
           description: str = "", env: dict[str, str] | None = None) -> int:
    """ひな形を作る。設定は変えない。"""
    env = dict(os.environ) if env is None else env
    if not modules._NAME.match(name):
        print(f"❌ 名前は英小文字で始め、英小文字・数字・- で書く（31文字まで）: {name}")
        return 1
    home = config_home(env)
    try:
        modules.register_user_modules(home / "modules")
    except modules.ModuleError as e:
        print(f"❌ モジュールを読めない: {e}")
        return 1
    if name in modules.known():
        print(f"❌ モジュール「{name}」はもうある（{modules.known()[name].path}）")
        return 1
    folder = (modules.BUILTIN_DIR if builtin else home / "modules") / name
    if folder.exists():
        print(f"❌ フォルダがもうある: {folder}")
        return 1
    port = free_port({spec.port for spec in modules.known().values() if spec.port is not None} | {8786}) \
        if process else 0
    for relative, text in files_for(name, ai=ai, process=process, label=label, description=description,
                                    port=port).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # 作ったものが読めるか確かめる（読めなければ、ひな形の誤り）
    try:
        modules.load_spec(folder, builtin=builtin)
    except modules.ModuleError as e:
        print(f"❌ 作ったひな形を読めない（Kei Agent の誤り）: {e}")
        return 1
    print(f"✅ モジュール「{name}」のひな形を作った: {folder}")
    for relative in sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file()):
        print(f"  - {relative}")
    group = " --group agents" if process else ""
    print("\nこのあとやること:")
    print("  - module.toml の description と、module.py の答え方を書く")
    print(f"  - テストを動かす: uv run{group} kei-agent module test {name}")
    print(f"  - オンにする: uv run kei-agent module add {name}（そのあとにやることは add が並べる）")
    if process:
        print(f"  - 担当プロセスを手元で動かす: uv run --group agents kei-agent-module {name}（番地 {port}）")
    return 0


def run_tests(name: str, args: list[str], *, env: dict[str, str] | None = None) -> int:
    """そのモジュールの tests/ を、柵（kei_agent.testing.plugin）を付けて pytest で動かす。"""
    env = dict(os.environ) if env is None else env
    try:
        modules.register_user_modules(config_home(env) / "modules")
    except modules.ModuleError as e:
        print(f"❌ モジュールを読めない: {e}")
        return 1
    spec = modules.known().get(name)
    if spec is None:
        print(f"❌ 知らないモジュール: {name}（kei-agent module list で見る）")
        return 1
    tests = spec.path / "tests"
    if not tests.is_dir():
        where = "リポジトリの tests/（uv run python -m pytest）" if spec.builtin else f"{tests}"
        print(f"❌ モジュール「{name}」のテストのフォルダが無い（{where}）")
        return 1
    # 設定ファイルは読まない（-c に空のもの）。非同期のテストをそのまま書けるように asyncio_mode=auto
    command = [sys.executable, "-m", "pytest", "-c", os.devnull, "--rootdir", str(tests), "-p",
               "kei_agent.testing.plugin", "-o", "asyncio_mode=auto", "--import-mode=importlib", str(tests), *args]
    try:
        return subprocess.run(command, check=False, env=env).returncode
    except OSError as e:
        print(f"❌ pytest を動かせない: {e}（uv sync でそろえる）")
        return 1
