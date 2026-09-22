"""Codex App connector の agent ごとの許可境界。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppPolicy:
    """永続化しない論理 App 名と、agent の書込み方針。"""

    agent: str
    app_names: frozenset[str]
    read_only: bool


POLICIES: dict[str, AppPolicy] = {
    "course": AppPolicy("course", frozenset({"Box", "Notion"}), False),
    "work": AppPolicy("work", frozenset({"Microsoft Outlook Email", "Microsoft Outlook Calendar"}), True),
    # 研究は App connector を使わず、scoped Notion gateway と W&B MCP だけを使う。
    "research": AppPolicy("research", frozenset(), False),
}


def policy_for(agent: str) -> AppPolicy:
    try:
        return POLICIES[agent]
    except KeyError:
        raise ValueError(f"未知のagentです: {agent}") from None
