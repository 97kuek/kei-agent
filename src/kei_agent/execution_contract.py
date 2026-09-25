"""Provider に依存しない一回の agent 実行条件。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kei_agent.config import AGENT_PLUGINS
from kei_agent.model_policy import ResolvedModel, UseCase

if TYPE_CHECKING:
    from kei_agent.config import Config
    from kei_agent.runner import ExecutionRequest
    from kei_agent.themes import Workspace


@dataclass(frozen=True)
class ExecutionContract:
    workspace: Workspace
    recipe: ResolvedModel
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


def required_capabilities(request: ExecutionRequest) -> frozenset[str]:
    read_only = is_read_only(request)
    required = {"filesystem.read", "filesystem.deny_read", "network.domain_allowlist"}
    if not read_only:
        required.add("filesystem.write_scope")
    if request.recipe.actor == "research" and not read_only:
        required.add("mcp.allowlist")
    return frozenset(required)


def resolve_contract(config: Config, request: ExecutionRequest) -> ExecutionContract:
    actor = request.recipe.actor
    prompt_path = (request.workspace.system_prompt or
                   (config.repo_root / "prompts" / f"{actor}.md" if actor in {"course", "work"}
                    else config.system_prompt_path))
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    skill_dir = config.agent_plugin_dir(actor) / "skills" if actor in AGENT_PLUGINS else None
    read_only = is_read_only(request)
    return ExecutionContract(
        workspace=request.workspace,
        recipe=request.recipe,
        prompt_text=prompt_text,
        prompt_version=prompt_fingerprint(prompt_text, skill_dir),
        skill_dir=skill_dir,
        capabilities=required_capabilities(request),
        read_only=read_only,
    )
