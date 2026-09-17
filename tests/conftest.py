from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ezra.config import Config, REPO_ROOT
from ezra.store import Store


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        research_root=tmp_path / "research",
        state_dir=tmp_path / "state",
        repo_root=REPO_ROOT,
        allowed_user_id="UME",
        allowed_domains=("export.arxiv.org",),
        allow_write=(tmp_path / "cache",),
    )


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)


@pytest.fixture
def backlog_config(config: Config, tmp_path: Path, monkeypatch) -> Config:
    """backlog をリポジトリではなく tmp に書くための設定。"""
    fake_repo = tmp_path / "repo"
    (fake_repo / "docs").mkdir(parents=True)
    return replace(config, repo_root=fake_repo)
