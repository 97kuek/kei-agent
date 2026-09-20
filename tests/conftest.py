from __future__ import annotations

from pathlib import Path

import pytest

from kei_agent.config import REPO_ROOT, Config
from kei_agent.store import Store


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        research_root=tmp_path / "research",
        course_root=tmp_path / "course",
        state_dir=tmp_path / "state",
        repo_root=REPO_ROOT,
        allowed_user_id="UME",
        allowed_domains=("export.arxiv.org",),
        allow_write=(tmp_path / "cache",),
        deny_read=(tmp_path / "secrets",),
    )


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)
