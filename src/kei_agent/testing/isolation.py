"""テストが本物に触れないための差し替え。pytest の monkeypatch か、同じ呼び方の Patches に対して使う。

- 本物の秘密情報を環境変数から外す（開発機のシェルには本物の鍵が入っていることがある）
- 本物の状態（~/.local/state/kei-agent）を開こうとしたら落とす。置き場所を書かない設定も一時フォルダを指す
- 本物の launchd の担当を起動し直さない
- 用途の分類器（本物の AI を動かす）の代わりに、モジュールの default_use_case を返す
"""

from __future__ import annotations

import os
from pathlib import Path

from kei_agent.configuration import config as config_module
from kei_agent.execution import model_classifier, updates
from kei_agent.framework import modules
from kei_agent.storage.store import Store

# これらで始まる名前と、モジュールの module.toml の [secrets] に書いてある名前は、テストのあいだ環境変数から外す
SECRET_PREFIXES = ("TOGGL_", "NOTION_", "SLACK_", "KEI_AGENT_", "BOX_", "WANDB_", "OPENAI_", "ANTHROPIC_")
# 本物の状態の置き場所（差し替える前の既定）
REAL_STATE = Path(config_module.DEFAULT_PATHS["state_dir"]).expanduser().resolve()
REAL_CLASSIFY = model_classifier.classify_module


class Patches:
    """monkeypatch と同じ setattr / setenv / delenv を持ち、undo で元に戻す（pytest の外でも使えるように）。"""

    def __init__(self):
        self._undo: list = []

    def setattr(self, target, name: str, value) -> None:
        old = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, old))
        setattr(target, name, value)

    def setenv(self, name: str, value: str) -> None:
        old = os.environ.get(name)
        self._undo.append(lambda: _put_env(name, old))
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        old = os.environ.pop(name)
        self._undo.append(lambda: _put_env(name, old))

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


def _put_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def secret_names() -> set[str]:
    """知っているモジュール（組み込みと、読み込んだ利用者のもの）と本体が要る秘密情報の名前。"""
    return {secret.name for _, secret in modules.secrets(list(modules.known()))}


def strip_secrets(patch) -> None:
    names = secret_names()
    for name in list(os.environ):
        if name.startswith(SECRET_PREFIXES) or name in names:
            patch.delenv(name, raising=False)


def stub_restarts(patch) -> list[str]:
    """本物の launchd の担当を起動し直さない。起動し直そうとした名前を返す（あとで確かめられるように）。"""
    restarted: list[str] = []
    patch.setattr(updates, "restart_service", lambda name: restarted.append(name) or True)
    return restarted


def guard_state(patch, root: Path) -> None:
    """置き場所を書かない設定は root の下を指すようにし、それでも本物の状態を開こうとしたら落とす。"""
    patch.setattr(config_module, "DEFAULT_PATHS", {name: str(root / name) for name in config_module.DEFAULT_PATHS})
    opened = Store.__init__

    def guarded(self, path: Path):
        if Path(path).expanduser().resolve().is_relative_to(REAL_STATE):
            raise AssertionError(f"テストから本物の状態を開こうとしました: {path}")
        opened(self, path)

    patch.setattr(Store, "__init__", guarded)


def stub_classifier(patch) -> None:
    """用途の分類器を動かさず、モジュールの default_use_case を使う（本物は軽いモデルを動かす）。"""
    async def default(_config, _store, spec, prompt: str, *, provider=None):
        return spec.actor.default_use_case

    patch.setattr(model_classifier, "classify_module", default)
