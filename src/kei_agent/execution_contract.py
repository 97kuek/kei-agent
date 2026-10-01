"""Provider に依存しない一回の agent 実行条件。どこまで触れるかは制限の表（agent_policy.py）から決める。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent.agent_policy import AgentPolicy, policy_of
from kei_agent.model_policy import ResolvedModel, UseCase

if TYPE_CHECKING:
    from kei_agent.configuration.config import Config
    from kei_agent.runner import ExecutionRequest
    from kei_agent.workspaces.themes import Workspace


@dataclass(frozen=True)
class ExecutionContract:
    workspace: Workspace
    recipe: ResolvedModel
    policy: AgentPolicy
    prompt_text: str
    prompt_version: str
    skill_dir: Path | None
    capabilities: frozenset[str]
    read_only: bool
    # 担当のほかに渡す共通の skill（リポジトリの plugins/<名前>/skills。Notion を使える担当には notion）
    shared_skill_dirs: tuple[Path, ...] = ()

    @property
    def skill_dirs(self) -> tuple[Path, ...]:
        """この実行で渡す skill の置き場すべて（担当のものと共通のもの）。"""
        return (*((self.skill_dir,) if self.skill_dir is not None else ()), *self.shared_skill_dirs)


def prompt_fingerprint(prompt_text: str, *skill_dirs: Path | None) -> str:
    """会話の起動時に固定される指示と skill 内容の版。"""
    digest = hashlib.sha256(prompt_text.encode("utf-8"))
    for skill_dir in skill_dirs:
        if skill_dir is None or not skill_dir.is_dir():
            continue
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(skill_dir)).encode("utf-8"))
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def is_read_only(request: ExecutionRequest) -> bool:
    """呼び出し元が取り落としても、振り分け・分類の recipe は書込み実行にしない。"""
    return request.read_only or request.recipe.actor == "router" or request.recipe.use_case is UseCase.ROUTING


def required_capabilities(policy: AgentPolicy) -> frozenset[str]:
    """provider がこの実行で強制できないといけない制限。"""
    required = {"filesystem.deny_read"}
    if policy.files != "none":
        required.add("filesystem.read")
    if policy.files == "write":
        required.add("filesystem.write_scope")
    if policy.shell:
        required.add("network.domain_allowlist")
    if policy.notion != "none":
        required.add("mcp.allowlist")
    if policy.connectors:
        required.add("app.allowlist")
    return frozenset(required)


def prompt_path(config: Config, policy: AgentPolicy, workspace: Workspace) -> Path:
    """指示書。作業場が持つもの（振り分け・分類の router.md）が優先。利用者が差し替えていれば、そちら。

    モジュールの担当の指示書は、そのモジュールのフォルダにある。
    """
    return workspace.system_prompt or config.prompt_file(policy.prompt, module=policy.name)


def prompt_text(config: Config, path: Path, profile: bool = True) -> str:
    """指示書の本文。会話する担当には、最後に利用者のプロフィール（話し方、所属、興味など）を差し込む。

    JSON だけを返す係（振り分け・分類、知識の選別・要約など）は、作業場の profile を False にして差し込まない。
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if profile and (about := config.profile_text):
        text = f"{text.rstrip()}\n\n## 依頼者のプロフィール\n\n{about}\n"
    return text


def skill_dir(config: Config, policy: AgentPolicy) -> Path | None:
    return config.agent_plugin_dir(policy.name) / "skills" if policy.plugin else None


# 共通の skill の置き場（リポジトリの plugins/<名前>/。Claude には plugin、Codex には skills を渡す）
SHARED_PLUGINS = "plugins"


def shared_skill_dirs(config: Config, policy: AgentPolicy) -> tuple[Path, ...]:
    """担当のほかに渡す共通の skill。Notion を使える担当には、既存のページの書式を保つ skill（plugins/notion）。"""
    return (config.repo_root / SHARED_PLUGINS / "notion" / "skills",) if policy.notion != "none" else ()


def _within_reach(config: Config, policy: AgentPolicy) -> AgentPolicy:
    """Notion のホームを書いていない担当には、届かない Notion の道具を渡さない（config.toml の [notion]）。"""
    if policy.notion != "none" and policy.name not in config.notion.client_homes():
        return replace(policy, notion="none")
    return policy


def prompt_version(config: Config, actor: str, workspace: Workspace | None = None) -> str:
    """その担当の会話の指示・skill の版。変わったら、古い会話を再開しない。"""
    policy = _within_reach(config, policy_of(actor))
    skills = (skill_dir(config, policy), *shared_skill_dirs(config, policy))
    if workspace is None:
        return prompt_fingerprint(prompt_text(config, config.prompt_file(policy.prompt, module=policy.name)), *skills)
    return prompt_fingerprint(prompt_text(config, prompt_path(config, policy, workspace), workspace.profile), *skills)


def resolve_contract(config: Config, request: ExecutionRequest) -> ExecutionContract:
    read_only = is_read_only(request)
    policy = _within_reach(config, policy_of(request.recipe.actor, request.recipe.use_case, read_only=read_only))
    text = prompt_text(config, prompt_path(config, policy, request.workspace), request.workspace.profile)
    skills = skill_dir(config, policy)
    shared = shared_skill_dirs(config, policy)
    return ExecutionContract(
        workspace=request.workspace,
        recipe=request.recipe,
        policy=policy,
        prompt_text=text,
        prompt_version=prompt_fingerprint(text, skills, *shared),
        skill_dir=skills,
        capabilities=required_capabilities(policy),
        read_only=read_only,
        shared_skill_dirs=shared,
    )
