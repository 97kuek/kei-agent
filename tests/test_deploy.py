"""launchd から動かすための、起動ファイルと手順の確認。"""

from __future__ import annotations

import shutil

import pytest

from kei_agent.config import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"


def test_notion_gateway_has_launchd_files():
    assert (DEPLOY / "run-notion-gateway.sh").exists()
    assert (DEPLOY / "com.kei-agent.notion-gateway.plist.template").exists()
    assert "notion-gateway" in (DEPLOY / "install.sh").read_text(encoding="utf-8")


def test_notion_gateway_runs_only_from_the_common_secrets():
    """gateway は NOTION_TOKEN を読む。ほかのエージェントの秘密情報は読まない。"""
    script = (DEPLOY / "run-notion-gateway.sh").read_text(encoding="utf-8")
    assert "kei-agent-notion-gateway" in script
    assert "kei-agent-work.zsh" not in script and "kei-agent-course.zsh" not in script


def test_deploy_readme_tells_how_to_make_the_gateway_token():
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "KEI_AGENT_NOTION_GATEWAY_TOKEN" in readme
    assert "deploy/install.sh notion-gateway" in readme


def test_no_secret_is_written_into_the_repository():
    """手順に本物のトークンを貼らない（例は ... のまま）。"""
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "ntn_..." in readme
    assert "openssl rand -hex 32" in readme


def test_every_launchd_script_trims_its_own_launchd_log():
    """launchd の出力は回らない。どのプロセスも起動のたびに、自分の plist が書く先を切り詰める。"""
    import re

    for script in sorted(DEPLOY.glob("run*.sh")):
        body = script.read_text(encoding="utf-8")
        label = "assistant" if script.name == "run.sh" else script.stem.removeprefix("run-")
        plist = (DEPLOY / f"com.kei-agent.{label}.plist.template").read_text(encoding="utf-8")
        log = re.search(r"__LOG_DIR__/([\w.-]+)</string>", plist).group(1)
        assert 'source "$REPO/deploy/_common.sh"' in body, script.name
        assert f"trim_launchd_log {log}" in body, script.name


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


@pytest.mark.skipif(shutil.which("zsh") is None, reason="起動スクリプトは macOS の zsh で動く")
def test_dropping_notion_secrets_keeps_the_gateway_master(tmp_path):
    """消すのは Notion の鍵だけ。ゲートウェイの親の合言葉は、client ごとの合言葉を作るのに要る。"""
    import subprocess

    probe = (f'source "{DEPLOY / "_common.sh"}"; drop_notion_secrets; '
             'print -r -- "${NOTION_TOKEN-unset} ${NOTION_COURSE_TOKEN-unset} ${KEI_AGENT_NOTION_GATEWAY_TOKEN-unset}"')
    result = subprocess.run(["zsh", "-c", probe], capture_output=True, text=True, check=True, env={
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "NOTION_TOKEN": "ntn_x", "NOTION_COURSE_TOKEN": "ntn_y", "KEI_AGENT_NOTION_GATEWAY_TOKEN": "master"})
    assert result.stdout.split() == ["unset", "unset", "master"]


def test_readme_no_longer_asks_for_a_course_notion_token():
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "NOTION_COURSE_TOKEN" not in readme
