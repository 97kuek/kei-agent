"""launchd から動かすための、起動ファイルと手順の確認。"""

from __future__ import annotations

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
