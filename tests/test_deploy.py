"""launchd から動かすための、起動ファイルと手順の確認。"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from kei_agent.configuration.config import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"
# 同じ形の担当。どれも deploy/run-agent.sh <名前> で起動する。常駐のプロセスを持つモジュールで、
# 共通の kei-agent-module <名前> で動く（研究もモジュール）
AGENTS = ("course", "notion", "research", "voice", "work")
# install.sh で登録できるもの（名前なしは本体）
# 本体（""）・担当・MCP のトンネル
INSTALLABLE = ("", *AGENTS, "tunnel")
ZSH = shutil.which("zsh")
needs_zsh = pytest.mark.skipif(ZSH is None, reason="起動スクリプトは macOS の zsh で動く")


def _plist(name: str) -> dict:
    """install.sh が登録する plist（`install.sh <名前> print` で、登録せずに作ったもの）。"""
    result = subprocess.run([ZSH, str(DEPLOY / "install.sh"), *([name] if name else []), "print"],
                            capture_output=True, text=True, check=True)
    return plistlib.loads(result.stdout.encode())


def _launches() -> dict[str, tuple[str, list[str], str]]:
    """plist ごとの（起動スクリプト、渡す引数、launchd の出力先のファイル名）。"""
    found = {}
    repo = str(REPO_ROOT)
    for name in INSTALLABLE:
        plist = _plist(name)
        shell, script, *args = plist["ProgramArguments"]
        assert shell == "/bin/zsh", name
        found[plist["Label"]] = (script.removeprefix(f"{repo}/deploy/"), args, Path(plist["StandardOutPath"]).name)
    return found


def test_the_notion_gateway_is_a_module_process_and_the_readme_holds_no_secret():
    """Notion のゲートウェイは Notion のモジュールの常駐のプロセス（A2A ではない）。起動は担当と同じ run-agent.sh。

    手順には合言葉の作り方を書き、本物のトークンは貼らない（例は ... のまま）。講義用の Notion の鍵はもう求めない。
    """
    assert not (DEPLOY / "run-notion-gateway.sh").exists()
    assert "deploy/install.sh notion" in (DEPLOY / "install.sh").read_text(encoding="utf-8")
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "KEI_AGENT_NOTION_GATEWAY_TOKEN" in readme and "openssl rand -hex 32" in readme and "ntn_..." in readme
    assert "deploy/install.sh notion" in readme and "notion-gateway" not in readme
    assert "NOTION_COURSE_TOKEN" not in readme


@needs_zsh
def test_every_agent_starts_from_one_script():
    """担当は同じ形。plist は run-agent.sh に自分の名前を渡す（ラベルとログの置き場所は名前のまま）。"""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    logs = Path.home() / "Library" / "Logs" / "kei-agent"
    for name in AGENTS:
        plist = _plist(name)
        assert plist["Label"] == f"com.kei-agent.{name}"
        assert plist["ProgramArguments"] == ["/bin/zsh", f"{REPO_ROOT}/deploy/run-agent.sh", name]
        assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(logs / f"{name}-launchd.log")
        assert plist["KeepAlive"] is True and plist["ThrottleInterval"] == 30
        # モジュールの担当は、pyproject.toml に自分の名前を持たない（共通の起動コマンドと、担当に共通のグループ）
        assert f"kei-agent-{name}" not in project["project"]["scripts"]
        assert name not in project["dependency-groups"]
    assert "kei-agent-module" in project["project"]["scripts"] and "agents" in project["dependency-groups"]


@needs_zsh
def test_every_launchd_script_is_started_by_a_plist_and_trims_its_own_log():
    """起動スクリプトは plist から呼ばれるものだけ（エージェントごとの写しを残さない）。

    launchd の出力は回らない。どのプロセスも起動のたびに、自分の plist が書く先を切り詰める。
    """
    launches = _launches()
    assert {script for script, _, _ in launches.values()} == {path.name for path in DEPLOY.glob("run*.sh")}
    for label, (script, args, log) in launches.items():
        body = (DEPLOY / script).read_text(encoding="utf-8")
        assert 'source "$REPO/deploy/_common.sh"' in body, label
        if args:
            # 名前を受け取る run-agent.sh は、名前から書く先を決める
            assert log == f"{args[0]}-launchd.log", label
            assert 'trim_launchd_log "$AGENT-launchd.log"' in body, label
        else:
            assert f"trim_launchd_log {log}" in body, label


def test_only_the_gateway_keeps_the_notion_token():
    """Notion の鍵はゲートウェイ（Notion のモジュール）だけが持つ。ほかは共通の秘密情報を読んだあとで消す（~/.config は直さない）。"""
    common = (DEPLOY / "_common.sh").read_text(encoding="utf-8")
    assert "unset NOTION_TOKEN NOTION_COURSE_TOKEN" in common and "NOTION_MODULE=notion" in common
    for script in sorted(DEPLOY.glob("run*.sh")):
        body = script.read_text(encoding="utf-8")
        assert "drop_notion_secrets" in body, script.name
        # 秘密情報をすべて読んでから消す（あとから読み直すと戻ってしまう）
        assert body.index("drop_notion_secrets") > body.rindex('source "$'), script.name


@needs_zsh
def test_dropping_notion_secrets_keeps_the_gateway_master(tmp_path):
    """消すのは Notion の鍵だけ。ゲートウェイの親の合言葉は、client ごとの合言葉を作るのに要る。"""
    probe = (f'source "{DEPLOY / "_common.sh"}"; drop_notion_secrets; '
             'print -r -- "${NOTION_TOKEN-unset} ${NOTION_COURSE_TOKEN-unset} ${KEI_AGENT_NOTION_GATEWAY_TOKEN-unset}"')
    result = subprocess.run([ZSH, "-c", probe], capture_output=True, text=True, check=True, env={
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "NOTION_TOKEN": "ntn_x", "NOTION_COURSE_TOKEN": "ntn_y", "KEI_AGENT_NOTION_GATEWAY_TOKEN": "master"})
    assert result.stdout.split() == ["unset", "unset", "master"]


# 起動スクリプトが呼ぶ uv の代わり（依存をそろえる sync の引数を書き出すだけ）
FAKE_UV = """#!/bin/sh
echo "uv $*"
"""
# 仮想環境の起動口の代わり。どこで、何として、どの環境変数で動かされたかを書き出す
FAKE_ENTRY = """#!/bin/sh
pwd -P
echo "$0 $*"
env
"""


def _run_agent(home: Path, *args: str, common: str | None = "", own: dict[str, str] | None = None,
               secrets: Path | None = None):
    """偽の HOME と、リポジトリの写し（deploy/ と偽の .venv）で deploy/run-agent.sh を動かす。

    common が None なら共通の秘密情報を置かない。secrets は設定（[paths] secrets）で選んだ置き場所で、
    渡すと、仮想環境の Python がその場所を答える。本物の担当は起動しない。
    """
    local = secrets or home / ".config" / "kei-agent" / "secrets"
    local.mkdir(parents=True, exist_ok=True)
    if common is not None:
        (local / "kei-agent.zsh").write_text(common, encoding="utf-8")
    for name, text in (own or {}).items():
        (local / f"kei-agent-{name}.zsh").write_text(text, encoding="utf-8")
    # 起動スクリプトの PATH は $HOME/.local/bin から探すので、本物の uv より先に見つかる
    uv = home / ".local" / "bin" / "uv"
    uv.parent.mkdir(parents=True, exist_ok=True)
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    repo = home / "repo"
    shutil.copytree(DEPLOY, repo / "deploy", dirs_exist_ok=True)
    # 担当プロセスを持つモジュール（知識など）は、modules/ の module.toml から見つける
    shutil.copytree(REPO_ROOT / "modules", repo / "modules", dirs_exist_ok=True)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    for name in ("module",):
        entry = venv_bin / f"kei-agent-{name}"
        entry.write_text(FAKE_ENTRY, encoding="utf-8")
        entry.chmod(0o755)
    if secrets is not None:
        python = venv_bin / "python"
        python.write_text(f'#!/bin/sh\n[ "$*" = "-m kei_agent.configuration.paths secrets" ] && echo "{secrets}"\n', encoding="utf-8")
        python.chmod(0o755)
    return subprocess.run([ZSH, str(repo / "deploy" / "run-agent.sh"), *args], capture_output=True, encoding="utf-8",
                          env={"PATH": "/usr/bin:/bin", "HOME": str(home)})


def _started(result: subprocess.CompletedProcess) -> tuple[str, str, str, dict[str, str]]:
    """偽の uv と偽の起動口が書き出した（uv の引数、作業ディレクトリ、起動したもの、環境変数）。"""
    assert result.returncode == 0, result.stderr
    uv, cwd, argv, *env = result.stdout.splitlines()
    return uv, cwd, argv, dict(line.split("=", 1) for line in env if "=" in line)


@needs_zsh
def test_only_the_notion_module_process_keeps_the_notion_token(tmp_path):
    """Notion の鍵が残るのは Notion のモジュールのプロセス（ゲートウェイ）だけ。

    エージェントごとのファイルに書いてあっても消す。ゲートウェイの親の合言葉は全員に残る。
    """
    common = 'export NOTION_TOKEN="ntn_x" KEI_AGENT_NOTION_GATEWAY_TOKEN="master"\n'
    *_, env = _started(_run_agent(tmp_path / "notion", "notion", common=common))
    assert env.get("NOTION_TOKEN") == "ntn_x"
    own = {"course": 'export NOTION_COURSE_TOKEN="ntn_y"\n'}
    *_, env = _started(_run_agent(tmp_path / "course", "course", common=common, own=own))
    assert "NOTION_TOKEN" not in env and "NOTION_COURSE_TOKEN" not in env
    assert env["KEI_AGENT_NOTION_GATEWAY_TOKEN"] == "master"


@needs_zsh
@pytest.mark.parametrize("args, common, message", [
    *[(args, "", "知らない担当です")
      for args in [(), ("",), ("assistant",), ("notion-gateway",), ("../work",), ("course work",)]],
    (("course",), None, "秘密情報のファイルがありません"),     # エージェントごとのファイルだけでは動かさない
])
def test_run_agent_refuses_unknown_names_and_missing_common_secrets(tmp_path, args, common, message):
    result = _run_agent(tmp_path, *args, common=common, own={"course": 'export CLAUDE_CONFIG_DIR="x"\n'})
    assert result.returncode == 1
    assert message in result.stderr
    assert result.stdout == ""  # uv まで行かない


@needs_zsh
@pytest.mark.parametrize("name", AGENTS)
def test_run_agent_starts_each_agent_from_the_common_secrets_alone(tmp_path, name):
    """担当ごとのファイルが無くても、共通のものだけで起動する。束ねとコマンドは名前で決まる。

    uv は依存をそろえるだけ（ほかのグループは消さない）で、担当は仮想環境から直に起動する（uv を親に残さない）。
    """
    uv, cwd, argv, env = _started(_run_agent(tmp_path, name))
    repo = (tmp_path / "repo").resolve()
    # 担当に共通のグループをそろえて、共通の起動コマンドに名前を渡す
    assert uv == "uv sync --frozen --inexact --quiet --group agents"
    assert argv == f"{tmp_path}/repo/.venv/bin/kei-agent-module {name}"
    assert cwd == str(repo)
    assert env["VIRTUAL_ENV"] == f"{tmp_path}/repo/.venv"
    assert env["PATH"].startswith(f"{tmp_path}/repo/.venv/bin:")


@needs_zsh
def test_run_agent_reads_its_own_secrets_after_the_common_ones(tmp_path):
    """エージェントごとのファイルは共通のもののあとに読み、上書きできる。ほかのエージェントのファイルは読まない。"""
    common = 'export CLAUDE_CODE_OAUTH_TOKEN="tok" CLAUDE_CONFIG_DIR="common"\n'
    own = {"course": 'unset CLAUDE_CODE_OAUTH_TOKEN\nexport CLAUDE_CONFIG_DIR="$HOME/.claude-personal"\n',
           "work": 'unset CLAUDE_CODE_OAUTH_TOKEN\nexport CLAUDE_CONFIG_DIR="$HOME/.claude-work"\n'}
    *_, env = _started(_run_agent(tmp_path, "course", common=common, own=own))
    assert env["CLAUDE_CONFIG_DIR"] == f"{tmp_path}/.claude-personal"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    *_, env = _started(_run_agent(tmp_path, "research", common=common, own=own))
    assert env["CLAUDE_CONFIG_DIR"] == "common"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


@needs_zsh
def test_run_agent_reads_secrets_from_the_configured_place(tmp_path):
    """秘密情報の置き場所は設定（[paths] secrets）で変えられる。既定の場所にあっても、選んだ場所のほうを読む。"""
    chosen = tmp_path / "dotfiles-local"
    (tmp_path / ".config" / "kei-agent" / "secrets").mkdir(parents=True)
    (tmp_path / ".config" / "kei-agent" / "secrets" / "kei-agent.zsh").write_text('export PICKED="default"\n')
    *_, env = _started(_run_agent(tmp_path, "course", common='export PICKED="chosen"\n', secrets=chosen))
    assert env["PICKED"] == "chosen"


# deploy/update.sh（1コマンドのデプロイ）。本物の launchd と uv には触らない

FAKE_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$HOME/calls.log"
"""


def _update_repo(home: Path, branch: str = "main") -> Path:
    """deploy/ の写しを入れた git リポジトリ（本番の checkout の代わり）と、偽の uv・launchctl・python。"""
    repo = home / "repo"
    shutil.copytree(DEPLOY, repo / "deploy")
    shutil.copytree(REPO_ROOT / "modules", repo / "modules")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", branch], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "first"], check=True)
    for tool in (home / ".local" / "bin" / "uv", home / ".local" / "bin" / "launchctl", repo / ".venv" / "bin" / "python"):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(FAKE_TOOL, encoding="utf-8")
        tool.chmod(0o755)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    return repo


def _update(repo: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run([ZSH, str(repo / "deploy" / "update.sh")], capture_output=True, encoding="utf-8",
                          env={"PATH": "/usr/bin:/bin", "HOME": str(home)})


def _printed(repo: Path, home: Path, *args: str) -> str:
    return subprocess.run([ZSH, str(repo / "deploy" / "install.sh"), *args, "print"], capture_output=True,
                          encoding="utf-8", check=True, env={"PATH": "/usr/bin:/bin", "HOME": str(home)}).stdout


@needs_zsh
def test_update_reinstalls_only_changed_plists_restarts_all_and_checks_the_version(tmp_path):
    repo = _update_repo(tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    (agents / "com.kei-agent.assistant.plist").write_text(_printed(repo, tmp_path), encoding="utf-8")
    # 雛形と違う（起動スクリプトが違う）担当は、登録し直す
    (agents / "com.kei-agent.course.plist").write_text(
        _printed(repo, tmp_path, "course").replace("run-agent.sh", "run-course.sh"), encoding="utf-8")

    result = _update(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                            encoding="utf-8", check=True).stdout.strip()
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert calls[0] == "uv sync --frozen --inexact --all-groups --quiet"
    assert "plist が変わったので登録し直します: course" in result.stdout
    assert "plist が変わったので登録し直します: assistant" not in result.stdout
    assert "登録されていません: research（使うなら deploy/install.sh research）" in result.stdout
    assert (agents / "com.kei-agent.course.plist").read_text(encoding="utf-8").strip() == \
        _printed(repo, tmp_path, "course").strip()
    uid = subprocess.run(["id", "-u"], capture_output=True, encoding="utf-8", check=True).stdout.strip()
    assert f"launchctl bootstrap gui/{uid} {agents / 'com.kei-agent.course.plist'}" in calls
    # 全部を起動し直してから（本体は最後）、新しい版で動いているかを確かめる
    kicks = [call for call in calls if call.startswith("launchctl kickstart")]
    assert kicks[-1] == f"launchctl kickstart -k gui/{uid}/com.kei-agent.assistant"
    assert calls[-1] == f"python -m kei_agent.operations.deploy_check {commit}"


@needs_zsh
def test_update_refuses_other_branches_and_unfinished_changes(tmp_path):
    repo = _update_repo(tmp_path / "a", branch="feature/x")
    result = _update(repo, tmp_path / "a")
    assert result.returncode == 1 and "main ではありません" in result.stderr
    assert not (tmp_path / "a" / "calls.log").exists()           # 何も動かさない

    repo = _update_repo(tmp_path / "b")
    (repo / "deploy" / "run.sh").write_text("# 書きかけ\n", encoding="utf-8")
    result = _update(repo, tmp_path / "b")
    assert result.returncode == 1 and "書きかけの変更があります" in result.stderr
    assert not (tmp_path / "b" / "calls.log").exists()
