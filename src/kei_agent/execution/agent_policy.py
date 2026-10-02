"""エージェントごとの制限の正本（docs/architecture.md の「柵」）。

Claude の設定（許可する道具、MCP、sandbox）と Codex の設定（権限 profile、App、MCP、Web 検索）は、
どちらもこの表から作る。provider で効く範囲が変わらないよう、同じ一覧をほかの場所に書かない。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from kei_agent.execution.model_policy import UseCase
from kei_agent.framework import modules

# Notion ゲートウェイの MCP の名前（全エージェント共通）。どのホームに届くかは、合言葉でゲートウェイが決める
NOTION_MCP = "kei-notion"
# 読むだけの実行に渡すゲートウェイの道具
NOTION_READ_TOOLS = ("read", "search", "query")

Access = Literal["none", "read", "write"]


@dataclass(frozen=True)
class CodexApp:
    """Codex App。ID はアカウントごとに違うので、実行のたびに表示名から引く。"""

    name: str
    # 使う道具（`<App の名前空間>.<道具>`）。ここに無い道具は、Codex がモデルに見せない
    tools: tuple[str, ...]


def _codex_tools(namespace: str, tools: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{namespace}.{tool}" for tool in tools)


@dataclass(frozen=True)
class Connector:
    """アカウントの連携（Claude は claude.ai、Codex は Codex App）。どちらも読む道具だけを使う。"""

    name: str
    # Claude: claude.ai の連携の名前（`mcp__<server>__<tool>`）と、許す道具
    claude_server: str
    claude_tools: tuple[str, ...]
    # Codex: 使う App と、その読む道具。無ければ Codex ではこの連携を使わない
    codex_apps: tuple[CodexApp, ...] = ()

    def claude_names(self) -> tuple[str, ...]:
        return tuple(f"mcp__{self.claude_server}__{tool}" for tool in self.claude_tools)


# モジュールの連携（大学の Box、仕事の Microsoft 365 など）は、そのモジュールの module.toml の [[actor.connectors]]


@dataclass(frozen=True)
class AgentPolicy:
    """1つのエージェントが、どこまで触れるか。"""

    name: str
    # 指示書（prompts/ の中）。作業場が別の指示書を持つとき（振り分け・分類）は、そちらを使う
    prompt: str
    # plugin/<name>/ の skill と、二の柵のフック
    plugin: bool
    # 作業場のファイル。none でも自分の作業場（前提のメモと skill）は読める。
    # write でも、書けるのは作業場と [sandbox] allow_write だけ
    files: Access
    # 作業場でのコマンド（sandbox の中）
    shell: bool
    # Web の検索と取得
    web: bool
    # Notion ゲートウェイ（利用者の名前はエージェントの名前）
    notion: Access
    # コマンドの通信（どこへでも出られる）。会社のデータを読む実行役と、読むだけの実行には無い
    network: bool = False
    connectors: tuple[Connector, ...] = ()
    # 1回の上限時間（分）。None なら config.toml の run_timeout_minutes
    timeout_minutes: int | None = None

    def narrowed(self, read_only: bool) -> AgentPolicy:
        """読むだけの実行では、書く・動かす手段を外す（連携は、もとから読む道具だけ）。"""
        if not read_only:
            return self
        return replace(self, files=_read(self.files), shell=False, network=False, notion=_read(self.notion))

    @property
    def codex_apps(self) -> tuple[CodexApp, ...]:
        return tuple(app for connector in self.connectors for app in connector.codex_apps)

    @property
    def notion_tools(self) -> tuple[str, ...] | None:
        """ゲートウェイで使える道具。None なら全部、空ならゲートウェイを渡さない。"""
        return {"none": (), "read": NOTION_READ_TOOLS, "write": None}[self.notion]


def _read(access: Access) -> Access:
    return "read" if access == "write" else access


POLICIES: dict[str, AgentPolicy] = {
    # 振り分け・分類。材料はプロンプトで渡すので、読むだけで道具も持たない
    "router": AgentPolicy("router", "router.md", plugin=False, files="read", shell=False, web=False,
                          notion="none"),
}


def module_policy(spec: modules.ModuleSpec) -> AgentPolicy:
    """モジュールの実行役の制限。道具は線から決まる（module.toml の [actor] data）。

    自分のデータ（own）を読む実行役は、作業場の読み書き・コマンド・Web・コマンドの通信をすべて使える。
    会社のデータ（company）を読む実行役は、作業場の読み書きとコマンドだけ（外へ出られない）。
    連携は、書いてある（読む）道具だけを使える。
    """
    assert spec.actor is not None
    actor = spec.actor
    connectors = tuple(
        Connector(c.name, c.claude_server, c.claude_tools,
                  tuple(CodexApp(app.name, _codex_tools(app.namespace, app.tools)) for app in c.codex_apps))
        for c in actor.connectors)
    outward = actor.data != "company"
    return AgentPolicy(spec.name, actor.prompt, plugin=actor.plugin, files="write", shell=True,
                       web=outward, notion=actor.notion, connectors=connectors,
                       timeout_minutes=actor.timeout_minutes, network=outward)


def policy_of(actor: str, use_case: UseCase | str | None = None, *, read_only: bool = False) -> AgentPolicy:
    """実行の制限。振り分け・分類の用途は、どの担当のものでも道具を持たない router にする。"""
    name = "router" if use_case is UseCase.ROUTING or actor == "router" else actor
    spec = modules.known().get(name)
    if name in POLICIES:
        policy = POLICIES[name]
    elif spec is not None and spec.actor is not None:
        policy = module_policy(spec)
    else:
        raise ValueError(f"未知のagentです: {actor}")
    return policy.narrowed(read_only or name == "router")
