"""モジュールへの取り次ぎ: 取り込み・予定・材料・出来事・App Home・ボタン・スラッシュコマンド・リアクション。

Assistant に混ぜて使う。self.slack、self.store、self.config などは Assistant のもの。
"""

from __future__ import annotations

import asyncio
import logging

from kei_agent.conversation.request import Request
from kei_agent.conversation.slack_text import (
    FAILED_PREFIX,
    NIGHT_REACTION,
    clean_text,
)
from kei_agent.framework import modules
from kei_agent.storage.notion import NotionError
from kei_agent.workspaces import themes
from kei_agent.workspaces.themes import ChannelKind

log = logging.getLogger(__name__)


class ModuleBridge:
    def _own_night_reaction(self, event: dict) -> bool:
        """依頼者が自分のメッセージに 🌙 をつけた（外した）か。"""
        item = event.get("item") or {}
        return (event.get("reaction") == NIGHT_REACTION and item.get("type") == "message"
                and self.is_allowed(event.get("user")) and event.get("item_user") == event.get("user"))

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

    async def module_reaction(self, event: dict, added: bool) -> bool:
        """モジュールの投稿へのリアクション（朝の読みものへの 👍 など）。どれかのモジュールが扱ったら True。"""
        for name, module in self.modules.items():
            on_reaction = getattr(module, "on_reaction", None)
            if on_reaction is None:
                continue
            try:
                if await on_reaction(event, added):
                    return True
            except Exception:
                # 1つのモジュールが落ちても、ほかのモジュールと 🌙 は止めない
                log.exception("モジュール「%s」がリアクションを扱えませんでした", name)
                await self.notify_trouble(f"モジュール「{name}」がリアクションを扱えませんでした")
        return False

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

    def module_home(self) -> list[tuple[str, list[dict]]]:
        """App Home に並べる、モジュールの項目（class Module の home）。作れなかったモジュールは飛ばす。"""
        sections = []
        for name, module in self.modules.items():
            build = getattr(module, "home", None)
            if not callable(build):
                continue
            try:
                blocks = [block for block in build() or [] if isinstance(block, dict)]
            except Exception:
                log.exception("モジュール「%s」の App Home の項目を作れませんでした", name)
                continue
            if blocks:
                sections.append((modules.known()[name].label, blocks))
        return sections

    async def module_home_action(self, module: str, name: str, action: dict) -> bool:
        """App Home のモジュールの項目が押された（class Module の on_home_action）。扱ったら True。"""
        on_home_action = getattr(self.modules.get(module), "on_home_action", None)
        if not callable(on_home_action):
            return False
        try:
            await on_home_action(name, action)
        except Exception:
            log.exception("モジュール「%s」が App Home の操作（%s）を扱えませんでした", module, name)
            await self.notify_trouble(f"モジュール「{module}」が App Home の操作を扱えませんでした")
        return True

    def _module_target(self, value: str) -> tuple[object, str] | None:
        """モジュールの action_id / callback_id（modules.ACTION_PREFIX。api.MODULE_PREFIX と同じ）から、そのモジュールと名前。"""
        if not value.startswith(modules.ACTION_PREFIX):
            return None
        module, _, name = value.removeprefix(modules.ACTION_PREFIX).partition(":")
        found = self.modules.get(module)
        return (found, name) if found is not None and name else None

    async def module_action(self, body: dict) -> None:
        """モジュールの投稿のボタンなどが押された（class Module の on_action）。押せるのは依頼者だけ。"""
        if not self.is_allowed(body.get("user", {}).get("id")):
            return
        action = (body.get("actions") or [{}])[0]
        target = self._module_target(str(action.get("action_id") or ""))
        on_action = getattr(target[0], "on_action", None) if target else None
        if callable(on_action):
            await on_action(target[1], body)

    async def module_view(self, body: dict) -> dict | None:
        """モジュールの入力の画面が送られた（class Module の on_view）。欄の下に出す理由を返すと、画面は閉じない。"""
        view = body.get("view") or {}
        target = self._module_target(str(view.get("callback_id") or ""))
        on_view = getattr(target[0], "on_view", None) if target else None
        if not callable(on_view):
            return None
        if not self.is_allowed(body.get("user", {}).get("id")):
            first = next(iter(view.get("blocks") or [{}]), {}).get("block_id", "")
            return {first: "依頼者だけが使えます"} if first else None
        return await on_view(target[1], body)

    async def module_slash(self, name: str, body: dict) -> str:
        """モジュールのスラッシュコマンド（class Module の on_slash_command）。打った人にだけ見せる文を返す。"""
        if not self.is_allowed(str(body.get("user_id") or "")):
            return "この操作は利用できません"
        for module_name, spec in ((n, modules.known()[n]) for n in self.modules):
            if name in spec.slash_commands:
                return str(await self.modules[module_name].on_slash_command(name, body) or "")
        return "このコマンドを受け持つモジュールがありません"

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
        """頭（手の口）から頼まれた操作（class Module の head_action）。受け持つモジュールが無ければ ValueError。"""
        for module in self.modules.values():
            act = getattr(module, "head_action", None)
            if callable(act) and (done := await act(name, params)) is not None:
                if self.config.allowed_user_id:
                    # App Home に同じスイッチが出ているので、開いている画面を今の値で作り直す（Slack につないでいるとき）
                    try:
                        await self.publish_home(self.config.allowed_user_id)
                    except Exception:
                        log.warning("App Home を作り直せませんでした", exc_info=True)
                return done
        raise ValueError(f"「{name}」を受け持つモジュールがありません（オフかもしれません）")

    async def module_head_materials(self, days: int) -> dict[str, list[dict]]:
        """頭（手の口）に渡す材料（class Module の head_materials）。種類 → 項目。作れなかったモジュールは飛ばす。

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
                log.exception("モジュール「%s」の頭への材料を作れませんでした", name)
                continue
            if not isinstance(given, dict):
                log.warning("モジュール「%s」の頭への材料が辞書ではありません", name)
                continue
            for kind, items in given.items():
                if isinstance(items, list):
                    found.setdefault(str(kind), []).extend(item for item in items if isinstance(item, dict))
        return found

    async def on_reaction_added(self, event: dict) -> None:
        """自分のメッセージに 🌙 をつけると、夜間の Task になる。モジュールの投稿へのリアクションは、そのモジュールが扱う。"""
        if await self.module_reaction(event, added=True) or not self._own_night_reaction(event):
            return
        channel, ts = event["item"]["channel"], event["item"]["ts"]
        name = await self.channel_name(channel)
        try:
            ws = themes.resolve(self.config, name)
        except ValueError:
            return
        if ws.kind is not ChannelKind.THEME:
            return
        message = await self.fetch_message(channel, ts) or {}
        req = Request(channel, name, message.get("thread_ts") or ts, None, "")
        if self.notion is None:
            await self.post(req, f"{FAILED_PREFIX} Notion が設定されていないので、今夜の Task にできません")
            return
        text = clean_text(message.get("text", ""))
        title = (text.splitlines() or ["Slack からの Task"])[0][:60] or "Slack からの Task"
        link = await self.permalink(channel, ts)
        quoted = "\n".join(f"> {line}" for line in text.splitlines()) or "> （本文なし）"
        body = f"Slack で 🌙 をつけて作った Task。\n\n{quoted}\n\n元のメッセージ: {link}"
        try:
            task = await asyncio.to_thread(self.notion.create_night_task, title, name, link, body)
        except NotionError as e:
            await self.post(req, f"{FAILED_PREFIX} Notion に Task を作れなかったよ")
            await self.notify_trouble(f"🌙 の Task を Notion に作れませんでした: {e}")
            return
        await self.post(req, f"🌙 今夜の Task にしたよ: <{task.url}|{task.title}>")

    async def on_reaction_removed(self, event: dict) -> None:
        if (await self.module_reaction(event, added=False) or not self._own_night_reaction(event)
                or self.notion is None):
            return
        link = await self.permalink(event["item"]["channel"], event["item"]["ts"])
        try:
            await asyncio.to_thread(self.notion.cancel_night_task, link)
        except NotionError as e:
            await self.notify_trouble(f"🌙 を外した Task を Notion で取り消せませんでした: {e}")

