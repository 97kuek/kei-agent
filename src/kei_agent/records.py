"""モジュールだけの記録（窓口の records）。本体側の kei_agent.api と、担当側の kei_agent_a2a.api の両方から使う。

同じ SQLite（store.py の module_records）に、モジュールの名前ごとに分けて書く。担当プロセスと本体は同じ
ファイルを開くので、本体側で書いたもの（App Home のオン・オフなど）を担当側で読める。
"""

from __future__ import annotations

import json
import time

_KEEP = object()


class Records:
    """そのモジュールだけの記録。種類と鍵で1件、中身は JSON にできる辞書。

    keep_days を付けたものは、その日数を過ぎると毎晩の保守で消える（付けなければ、消すまで残る）。
    """

    def __init__(self, store, module: str):
        self._store = store
        self._module = module

    def put(self, kind: str, key: str, value: dict, *, keep_days: float | None = None) -> None:
        self._store.put_module_record(self._module, kind, key, json.dumps(value, ensure_ascii=False),
                                      _expires(keep_days))

    def get(self, kind: str, key: str) -> dict | None:
        row = self._store.module_record(self._module, kind, key)
        return json.loads(row["value"]) if row is not None else None

    def update(self, kind: str, key: str, *, keep_days: float | None | object = _KEEP, **changes) -> dict | None:
        """中身の一部を書き換える。keep_days を渡せば、残す日数も変える（None なら消すまで残す）。無ければ None。"""
        row = self._store.module_record(self._module, kind, key)
        if row is None:
            return None
        value = {**json.loads(row["value"]), **changes}
        expires = row["expires_at"] if keep_days is _KEEP else _expires(keep_days)
        self._store.put_module_record(self._module, kind, key, json.dumps(value, ensure_ascii=False), expires)
        return value

    def items(self, kind: str) -> list[dict]:
        """その種類の記録の中身（新しく書いた順）。"""
        return [json.loads(row["value"]) for row in self._store.module_records(self._module, kind)]

    def delete(self, kind: str, key: str) -> None:
        self._store.delete_module_record(self._module, kind, key)


def _expires(keep_days: float | None) -> float | None:
    return None if keep_days is None else time.time() + keep_days * 86400
