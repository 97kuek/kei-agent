from __future__ import annotations

from pathlib import Path

import pytest

from kei_agent import model_classifier, research
from kei_agent.config import REPO_ROOT, AgentProfile, Config
from kei_agent.store import Store


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        research_root=tmp_path / "research",
        agent_root=tmp_path / "kei-agent",
        course_root=tmp_path / "course",
        state_dir=tmp_path / "state",
        repo_root=REPO_ROOT,
        allowed_user_id="UME",
        allowed_domains=("export.arxiv.org",),
        allow_write=(tmp_path / "cache",),
        deny_read=(tmp_path / "secrets",),
        # 個別の unit test は既存経路の振る舞いを検証する。製品の config.toml は未選択で始まる。
        agent_profiles={name: AgentProfile(provider="claude")
                        for name in ("research", "course", "work", "router", "self_fix")},
    )


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)


@pytest.fixture(autouse=True)
def fake_model_classifier(monkeypatch):
    """通常の unit test は本物の CLI を起動せず、既存の用途判定だけを再現する。"""
    async def classify(_config, _store, prompt: str, **_kwargs):
        return research.use_case_for_prompt(prompt)[0]

    monkeypatch.setattr(model_classifier, "classify_research", classify)

    async def course(_config, _store, _prompt: str):
        from kei_agent.model_policy import UseCase
        return UseCase.COURSE_EXPLAIN

    async def work(_config, _store, _prompt: str):
        from kei_agent.model_policy import UseCase
        return UseCase.WORK_SINGLE_SOURCE

    monkeypatch.setattr(model_classifier, "classify_course", course)
    monkeypatch.setattr(model_classifier, "classify_work", work)
