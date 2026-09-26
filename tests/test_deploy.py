"""launchd から動かすための、起動ファイルと手順の確認。"""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from kei_agent.config import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"
# 同じ形の担当。どれも deploy/run-agent.sh <名前> で起動する
AGENTS = ("research", "course", "work", "knowledge", "voice")
# install.sh で登録できるもの（名前なしは本体）
INSTALLABLE = ("", *AGENTS, "notion-gateway")
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


def test_notion_gateway_has_launchd_files():
    assert (DEPLOY / "run-notion-gateway.sh").exists()
    assert (DEPLOY / "com.kei-agent.plist.template").exists()
    assert "notion-gateway" in (DEPLOY / "install.sh").read_text(encoding="utf-8")


def test_notion_gateway_runs_only_from_the_common_secrets():
    """gateway は NOTION_TOKEN を読む。ほかのエージェントの秘密情報は読まない。"""
    script = (DEPLOY / "run-notion-gateway.sh").read_text(encoding="utf-8")
    assert "kei-agent-notion-gateway" in script
    assert not re.search(r"kei-agent-[\w$]+\.zsh", script)


def test_deploy_readme_tells_how_to_make_the_gateway_token():
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "KEI_AGENT_NOTION_GATEWAY_TOKEN" in readme
    assert "deploy/install.sh notion-gateway" in readme


def test_no_secret_is_written_into_the_repository():
    """手順に本物のトークンを貼らない（例は ... のまま）。"""
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "ntn_..." in readme
    assert "openssl rand -hex 32" in readme


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
        # run-agent.sh は、グループ <名前> をそろえてから kei-agent-<名前> を動かす
        assert f"kei-agent-{name}" in project["project"]["scripts"]
        assert name in project["dependency-groups"]


@needs_zsh
def test_every_launchd_script_is_started_by_a_plist():
    """起動スクリプトは plist から呼ばれるものだけ（エージェントごとの写しを残さない）。"""
    assert {script for script, _, _ in _launches().values()} == {path.name for path in DEPLOY.glob("run*.sh")}


@needs_zsh
def test_every_launchd_script_trims_its_own_launchd_log():
    """launchd の出力は回らない。どのプロセスも起動のたびに、自分の plist が書く先を切り詰める。"""
    for label, (script, args, log) in _launches().items():
        body = (DEPLOY / script).read_text(encoding="utf-8")
        assert 'source "$REPO/deploy/_common.sh"' in body, label
        if args:
            # 名前を受け取る run-agent.sh は、名前から書く先を決める
            assert log == f"{args[0]}-launchd.log", label
            assert 'trim_launchd_log "$AGENT-launchd.log"' in body, label
        else:
            assert f"trim_launchd_log {log}" in body, label


def test_only_the_gateway_keeps_the_notion_token():
    """Notion の鍵はゲートウェイだけが持つ。ほかは共通の秘密情報を読んだあとで消す（~/.config は直さない）。"""
    common = (DEPLOY / "_common.sh").read_text(encoding="utf-8")
    assert "unset NOTION_TOKEN NOTION_COURSE_TOKEN" in common
    for script in sorted(DEPLOY.glob("run*.sh")):
        body = script.read_text(encoding="utf-8")
        if script.name == "run-notion-gateway.sh":
            assert "drop_notion_secrets" not in body
            continue
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


def _run_agent(home: Path, *args: str, common: str | None = "", own: dict[str, str] | None = None):
    """偽の HOME と、リポジトリの写し（deploy/ と偽の .venv）で deploy/run-agent.sh を動かす。

    common が None なら共通の秘密情報を置かない。本物の担当は起動しない。
    """
    local = home / ".config" / "zsh" / "local"
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
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    for name in AGENTS:
        entry = venv_bin / f"kei-agent-{name}"
        entry.write_text(FAKE_ENTRY, encoding="utf-8")
        entry.chmod(0o755)
    return subprocess.run([ZSH, str(repo / "deploy" / "run-agent.sh"), *args], capture_output=True, encoding="utf-8",
                          env={"PATH": "/usr/bin:/bin", "HOME": str(home)})


def _started(result: subprocess.CompletedProcess) -> tuple[str, str, str, dict[str, str]]:
    """偽の uv と偽の起動口が書き出した（uv の引数、作業ディレクトリ、起動したもの、環境変数）。"""
    assert result.returncode == 0, result.stderr
    uv, cwd, argv, *env = result.stdout.splitlines()
    return uv, cwd, argv, dict(line.split("=", 1) for line in env if "=" in line)


@needs_zsh
@pytest.mark.parametrize("args", [(), ("",), ("assistant",), ("notion-gateway",), ("../work",), ("course work",)])
def test_run_agent_refuses_an_unknown_name(tmp_path, args):
    result = _run_agent(tmp_path, *args)
    assert result.returncode == 1
    assert "知らない担当です" in result.stderr
    assert result.stdout == ""  # uv まで行かない


@needs_zsh
@pytest.mark.parametrize("name", AGENTS)
def test_run_agent_starts_each_agent_from_the_common_secrets_alone(tmp_path, name):
    """担当ごとのファイルが無くても、共通のものだけで起動する。束ねとコマンドは名前で決まる。

    uv は依存をそろえるだけ（ほかのグループは消さない）で、担当は仮想環境から直に起動する（uv を親に残さない）。
    """
    uv, cwd, argv, env = _started(_run_agent(tmp_path, name))
    repo = (tmp_path / "repo").resolve()
    assert uv == f"uv sync --frozen --inexact --quiet --group {name}"
    assert cwd == str(repo)
    assert argv == f"{tmp_path}/repo/.venv/bin/kei-agent-{name} "
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
def test_run_agent_drops_notion_secrets_from_every_file(tmp_path):
    """Notion の鍵は、エージェントごとのファイルに書いてあっても消す。ゲートウェイの親の合言葉は残す。"""
    common = 'export NOTION_TOKEN="ntn_x" KEI_AGENT_NOTION_GATEWAY_TOKEN="master"\n'
    own = {"course": 'export NOTION_COURSE_TOKEN="ntn_y"\n'}
    *_, env = _started(_run_agent(tmp_path, "course", common=common, own=own))
    assert "NOTION_TOKEN" not in env and "NOTION_COURSE_TOKEN" not in env
    assert env["KEI_AGENT_NOTION_GATEWAY_TOKEN"] == "master"


@needs_zsh
def test_run_agent_stops_without_the_common_secrets(tmp_path):
    """エージェントごとのファイルだけでは動かさない。"""
    result = _run_agent(tmp_path, "course", common=None, own={"course": 'export CLAUDE_CONFIG_DIR="x"\n'})
    assert result.returncode == 1
    assert "秘密情報のファイルがありません" in result.stderr
    assert result.stdout == ""


def test_readme_no_longer_asks_for_a_course_notion_token():
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "NOTION_COURSE_TOKEN" not in readme
