"""Provider に依存しない一回の agent 実行条件。どこまで触れるかは制限の表（agent_policy.py）から決める。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent.agent_policy import AgentPolicy, policy_of
from kei_agent.model_policy import ResolvedModel, UseCase

if TYPE_CHECKING:
    from kei_agent.config import Config
    from kei_agent.runner import ExecutionRequest
    from kei_agent.themes import Workspace


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


def prompt_fingerprint(prompt_text: str, skill_dir: Path | None) -> str:
    """会話の起動時に固定される指示と skill 内容の版。"""
    digest = hashlib.sha256(prompt_text.encode("utf-8"))
    if skill_dir is not None and skill_dir.is_dir():
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


# 利用者のプロフィールを差し込まない指示書（JSON だけを返す、振り分け・分類と、選別・要約の係）
NO_PROFILE = frozenset({"router.md", "knowledge-digest.md"})


def prompt_path(config: Config, policy: AgentPolicy, workspace: Workspace) -> Path:
    """指示書。作業場が持つもの（振り分け・分類の router.md）が優先。利用者が差し替えていれば、そちら。"""
    return workspace.system_prompt or config.prompt_file(policy.prompt)


def prompt_text(config: Config, path: Path) -> str:
    """指示書の本文。会話する担当には、最後に利用者のプロフィール（話し方、所属、興味など）を差し込む。"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    profile = config.profile_text
    if profile and path.name not in NO_PROFILE:
        text = f"{text.rstrip()}\n\n## 依頼者のプロフィール\n\n{profile}\n"
    return text


def skill_dir(config: Config, policy: AgentPolicy) -> Path | None:
    return config.agent_plugin_dir(policy.name) / "skills" if policy.plugin else None


def prompt_version(config: Config, actor: str, workspace: Workspace | None = None) -> str:
    """その担当の会話の指示・skill の版。変わったら、古い会話を再開しない。"""
    policy = policy_of(actor)
    path = config.prompt_file(policy.prompt) if workspace is None else prompt_path(config, policy, workspace)
    return prompt_fingerprint(prompt_text(config, path), skill_dir(config, policy))


def resolve_contract(config: Config, request: ExecutionRequest) -> ExecutionContract:
    read_only = is_read_only(request)
    policy = policy_of(request.recipe.actor, request.recipe.use_case, read_only=read_only)
    text = prompt_text(config, prompt_path(config, policy, request.workspace))
    skills = skill_dir(config, policy)
    return ExecutionContract(
        workspace=request.workspace,
        recipe=request.recipe,
        policy=policy,
        prompt_text=text,
        prompt_version=prompt_fingerprint(text, skills),
        skill_dir=skills,
        capabilities=required_capabilities(policy),
        read_only=read_only,
    )
