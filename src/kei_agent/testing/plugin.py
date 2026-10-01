"""pytest の plugin（モジュールのテストで読み込む）。

- 本物の秘密情報・状態・launchd・利用者のフォルダ・用途の分類器に触れないようにする（どのテストにも自動で効く）
- module_kit … ModuleKit を作る fixture（kit = module_kit(フォルダ, ...)。テストが終わったら戻す）

読み込み方: `pytest -p kei_agent.testing.plugin` か、いちばん上の conftest.py に
`pytest_plugins = ["kei_agent.testing.plugin"]`。
"""

import pytest

from kei_agent.framework import modules
from kei_agent.testing import isolation
from kei_agent.testing.kit import ModuleKit


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch):
    """開発機のシェルの本物の秘密情報を、テストのあいだ環境変数から外す（使うテストは monkeypatch.setenv で入れ直す）。"""
    isolation.strip_secrets(monkeypatch)


@pytest.fixture(autouse=True)
def no_real_restarts(monkeypatch):
    """本物の launchd の担当を起動し直さない（2026-09-26、テストを回すたびに本番の担当が起動し直されていた）。
    起動し直そうとした名前の一覧を返す。"""
    return isolation.stub_restarts(monkeypatch)


@pytest.fixture(autouse=True)
def no_user_modules(monkeypatch):
    """利用者のモジュールは、テストごとに空から始める（読んだものがほかのテストに残らない）。"""
    monkeypatch.setattr(modules, "_user", {})


@pytest.fixture(autouse=True)
def no_real_state(tmp_path_factory, monkeypatch):
    """置き場所を書かない設定も一時フォルダを指し、本物の状態（~/.local/state/kei-agent）を開こうとしたら落とす
    （2026-09-27、既定の置き場所のまま Store を開いたテストが、本番の SQLite の表を作り替えてしまった）。"""
    isolation.guard_state(monkeypatch, tmp_path_factory.mktemp("default-paths"))


@pytest.fixture(autouse=True)
def kei_agent_home(no_real_secrets, tmp_path_factory, monkeypatch):
    """利用者のフォルダ（~/.config/kei-agent）は、テストごとに空の設定だけのものにする（開発機の本物を読まない）。"""
    home = tmp_path_factory.mktemp("kei-agent-home")
    (home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_real_classifier(monkeypatch):
    """用途の分類器（本物は軽いモデルを動かす）の代わりに、モジュールの default_use_case を使う。本物を返す。"""
    isolation.stub_classifier(monkeypatch)
    return isolation.REAL_CLASSIFY


@pytest.fixture
def module_kit(tmp_path):
    """ModuleKit を作る関数（kit = module_kit(フォルダ, others=..., settings=...)）。テストが終わったら戻す。"""
    made: list[ModuleKit] = []

    def make(module, **options) -> ModuleKit:
        kit = ModuleKit(module, tmp_path / f"kit-{len(made)}", **options)
        made.append(kit)
        return kit

    yield make
    for kit in reversed(made):
        kit.close()
