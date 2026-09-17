"""設定の読み込み。

秘密情報（Slackのトークンなど）は環境変数から、それ以外は config.toml から読む。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


@dataclass(frozen=True)
class Config:
    research_root: Path
    state_dir: Path
    repo_root: Path
    allowed_user_id: str
    overview_channels: tuple[str, ...] = ("research-overview", "research-strategy")
    improve_channels: tuple[str, ...] = ("assistant-improve",)
    max_concurrent_runs: int = 2
    run_timeout_minutes: int = 30
    job_poll_seconds: int = 60
    job_parallel: int = 1
    model: str = ""
    allowed_domains: tuple[str, ...] = ()
    allow_write: tuple[Path, ...] = ()
    claude_bin: str = "claude"
    pueue_bin: str = "pueue"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "ezra.db"

    @property
    def plugin_dir(self) -> Path:
        return self.repo_root / "plugin"

    @property
    def system_prompt_path(self) -> Path:
        return self.repo_root / "prompts" / "system.md"

    @property
    def backlog_path(self) -> Path:
        return self.repo_root / "docs" / "backlog.md"


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    path = path or Path(env.get("EZRA_CONFIG", REPO_ROOT / "config.toml"))
    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)

    channels = data.get("channels", {})
    sandbox = data.get("sandbox", {})
    return Config(
        research_root=_expand(data.get("research_root", "~/research")),
        state_dir=_expand(data.get("state_dir", "~/.local/state/ezra")),
        repo_root=REPO_ROOT,
        allowed_user_id=env.get("EZRA_ALLOWED_USER_ID", ""),
        overview_channels=tuple(channels.get("overview", Config.overview_channels)),
        improve_channels=tuple(channels.get("improve", Config.improve_channels)),
        max_concurrent_runs=int(data.get("max_concurrent_runs", 2)),
        run_timeout_minutes=int(data.get("run_timeout_minutes", 30)),
        job_poll_seconds=int(data.get("job_poll_seconds", 60)),
        job_parallel=int(data.get("job_parallel", 1)),
        model=data.get("model", ""),
        allowed_domains=tuple(sandbox.get("allowed_domains", ())),
        allow_write=tuple(_expand(p) for p in sandbox.get("allow_write", ())),
        claude_bin=env.get("EZRA_CLAUDE_BIN", "claude"),
        pueue_bin=env.get("EZRA_PUEUE_BIN", "pueue"),
    )
