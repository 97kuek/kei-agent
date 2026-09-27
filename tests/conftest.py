from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeGitHub

from kei_agent import model_classifier, modules
from kei_agent.config import REPO_ROOT, AgentProfile, Config, NotionConfig, model_actors
from kei_agent.store import Store

# 本物の秘密情報・状態・launchd・利用者のフォルダに触れないための柵は、モジュールを作る人と同じもの
# （kei_agent.testing.plugin）を使う。用途の分類器だけは、研究の言葉を当てる下の fake_model_classifier にする
from kei_agent.testing.plugin import (  # noqa: F401
    kei_agent_home,
    module_kit,
    no_real_restarts,
    no_real_secrets,
    no_real_state,
    no_user_modules,
)

# 組み込みのモジュールのフォルダを、パッケージとして読めるようにしておく（from kei_agent_modules.knowledge import digest）
for _spec in modules.builtin().values():
    modules.package(_spec)


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
        agent_profiles={name: AgentProfile(provider="claude") for name in model_actors()},
        # 研究と大学は Notion のホームを持つ（作者の環境と同じ。ホームが無いときの試験は、ここを空にして行う）
        notion=NotionConfig(research_home="research-home", course_home="course-home"),
    )


@pytest.fixture
def store(config: Config) -> Store:
    return Store(config.db_path)


# 研究の用途の見分け方の代わり（本物は軽いモデルが module.toml の classify で選ぶ）。言葉で当てる
_RESEARCH_WORDS = (
    (("研究設計", "仮説", "実験計画", "手法選択", "比較設計", "厳密なレビュー"), "research_design"),
    (("比較", "結果", "考察", "差分", "レビュー"), "research_compare"),
    (("notion", "w&b", "wandb", "run", "metric", "artifact", "記録", "ログ", "一覧", "確認"), "research_extract"),
)


def research_use_case(prompt: str) -> str:
    text = prompt.strip()
    for words, use_case in _RESEARCH_WORDS:
        if any(word in text or word in text.lower() for word in words):
            return use_case
    return "research_execute"


@pytest.fixture(autouse=True)
def fake_model_classifier(monkeypatch):
    """通常の unit test は本物の CLI を起動せず、既存の用途判定だけを再現する。"""
    async def module(_config, _store, spec, prompt: str, *, provider=None):
        # 研究は言葉で当てる（以前の研究の分類器の代わり）。ほかのモジュールは default_use_case
        return research_use_case(prompt) if spec.name == "research" else spec.actor.default_use_case

    real_module = model_classifier.classify_module
    # モジュールの実行役（大学・仕事など）は、分類器を動かさずに default_use_case。確かめたいテストは戻り値で本物に戻す
    monkeypatch.setattr(model_classifier, "classify_module", module)
    return real_module


@pytest.fixture(autouse=True)
def fake_github(monkeypatch):
    """要望の issue 化（自己改善のモジュール）で、本物の gh（公開リポジトリ）と要約のモデルを動かさない。

    確かめたいテストは、引数に `fake_github` を書いて偽物を受け取る。
    """
    from kei_agent_modules.improve import issues

    github = FakeGitHub()
    monkeypatch.setattr(issues, "gh", github)

    async def summarize(_run_ai, _text: str, _has_secret):
        return issues.Summary("Kei Agent への要望", "- 要望の要約")

    monkeypatch.setattr(issues, "summarize", summarize)
    return github


@pytest.fixture(autouse=True)
def no_date_line(monkeypatch):
    """担当への依頼の先頭に付く今日の日付を、ふだんのテストでは空にする（依頼の本文だけを確かめられるように）。

    日付が付くことは test_assistant.py の専用のテストで確かめる。
    """
    from kei_agent import assistant

    monkeypatch.setattr(assistant, "today_line", lambda now=None: "")

