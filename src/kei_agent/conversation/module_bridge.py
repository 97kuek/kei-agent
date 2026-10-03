"""モジュールへの取り次ぎ: 取り込み・予定・材料・出来事・MCP 操作。

Assistant に混ぜて使う。self.slack、self.store、self.config などは Assistant のもの。
"""

from __future__ import annotations

import logging

from kei_agent.framework import modules

log = logging.getLogger(__name__)


class ModuleBridge:

    def _usable(self, name: str) -> bool:
        """そのモジュールを動かせるか（担当プロセスを持つなら、その住所がある。使っていない担当は黙って飛ばす）。"""
        return modules.known()[name].port is None or name in self.agents

    async def module_prepare(self, kind: str, day: str) -> list[str]:
        """Daily・振り返りの前の取り込み（class Module の prepare）。うまくいかなかったことの短い名前を返す。"""
        failed: list[str] = []
        for name, module in self.modules.items():
            prepare = getattr(module, "prepare", None)
            if prepare is None or not self._usable(name):
                continue
            try:
                failed += [str(label) for label in await prepare(kind, day) or []]
            except Exception:
                log.exception("モジュール「%s」の取り込みが落ちました", name)
                failed.append(f"{modules.known()[name].label}の取り込み")
        return failed

    async def module_agenda(self, days: int, kinds: frozenset[str] | None = None,
                            ) -> tuple[dict[str, list[dict]], list[str]]:
        """モジュールの予定（class Module の agenda）。読めたモジュールの名前 → 予定と、読めなかったモジュールの表示名。

        kinds を渡すと、その種類（meeting / class / due）だけを頼む（振り返りの締切のために、会議を AI で読まない）。
        読めなかった（None を返した・落ちた）モジュールは、予定が無いのとは分ける（予定カレンダーの行を
        「要確認」にしないため）。
        """
        found: dict[str, list[dict]] = {}
        failed: list[str] = []
        for name, module in self.modules.items():
            agenda = getattr(module, "agenda", None)
            if agenda is None or not self._usable(name):
                continue
            try:
                items = await agenda(days, kinds)
            except Exception:
                log.exception("モジュール「%s」の予定を読めませんでした", name)
                items = None
            if items is None:
                failed.append(modules.known()[name].label)
                continue
            found[name] = [item for item in items if isinstance(item, dict)
                           and (kinds is None or item.get("kind", "meeting") in kinds)]
        return found, failed


    def emit(self, kind: str, **fields) -> None:
        """出来事を配る（受け取るのは on_event を持つモジュール。声なら喋る）。投げっぱなしで、届かなくても
        呼んだ側は気にしない。空の中身（None と空文字）は外して渡す。
        """
        data = {key: value for key, value in fields.items() if value not in (None, "")}
        for name, module in self.modules.items():
            on_event = getattr(module, "on_event", None)
            if callable(on_event):
                self.spawn(self._deliver_event(name, on_event, kind, dict(data)))

    @staticmethod
    async def _deliver_event(name: str, on_event, kind: str, data: dict) -> None:
        try:
            await on_event(kind, data)
        except Exception:
            # 知らせを受け取れなかっただけで、配った側の仕事は終わっている
            log.exception("モジュール「%s」が出来事（%s）を受け取れませんでした", name, kind)







    async def module_material(self, now: float, skip: str = "") -> list[str]:
        """Daily と振り返りの材料に、モジュールが足す行（class Module の material）。作れなかったモジュールは飛ばす。"""
        lines: list[str] = []
        for name, module in self.modules.items():
            if name == skip:
                continue
            material = getattr(module, "material", None)
            if not callable(material):
                continue
            try:
                lines += [str(line) for line in await material(now) or []]
            except Exception:
                log.exception("モジュール「%s」の材料を作れませんでした", name)
        return lines

    async def module_head_action(self, name: str, params: dict) -> dict:
        """MCP クライアントから頼まれた操作（class Module の head_action）。受け持つモジュールが無ければ ValueError。"""
        for module in self.modules.values():
            act = getattr(module, "head_action", None)
            if callable(act) and (done := await act(name, params)) is not None:
                return done
        raise ValueError(f"「{name}」を受け持つモジュールがありません（オフかもしれません）")

    async def module_head_materials(self, days: int) -> dict[str, list[dict]]:
        """MCP クライアントに渡す材料（class Module の head_materials）。種類 → 項目。作れなかったモジュールは飛ばす。

        材料は手元の記録から作るもの（担当のプロセスに聞きに行かない）ので、担当の住所が無くても読む。
        """
        found: dict[str, list[dict]] = {}
        for name, module in self.modules.items():
            materials = getattr(module, "head_materials", None)
            if not callable(materials):
                continue
            try:
                given = await materials(days) or {}
            except Exception:
                log.exception("モジュール「%s」のMCP の材料を作れませんでした", name)
                continue
            if not isinstance(given, dict):
                log.warning("モジュール「%s」のMCP の材料が辞書ではありません", name)
                continue
            for kind, items in given.items():
                if isinstance(items, list):
                    found.setdefault(str(kind), []).extend(item for item in items if isinstance(item, dict))
        return found
