"""毎晩の保守: 古いファイルの整理と、研究データのバックアップ。

バックアップは ~/research を Git のリポジトリとして扱い、変更をコミットして push する。
Ezra の状態（SQLite と Notion の ID）は、SQL のテキストなどにして ~/research/_ezra_state/ に書き出してから含める。
準備は deploy/backup-init.sh で行う。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path

from ezra.config import Config
from ezra.themes import OVERVIEW_DIR

STATE_DIR = "_ezra_state"
# GitHub が受け付けない大きさ。これより大きいファイルはコミットしない
MAX_FILE_BYTES = 50 * 1024 * 1024
_EXCLUDE_BEGIN = "# ezra: 大きすぎるファイル（自動で書き換える）"
_EXCLUDE_END = "# ezra: ここまで"


def claude_project_dir_name(path: Path) -> str:
    """Claude Code がセッションの記録を置くディレクトリの名前（英数字以外を - にしたもの）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_projects_dir(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    base = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude"
    return base / "projects"


def remove_older_than(paths: list[Path], cutoff: float) -> int:
    removed = 0
    for p in paths:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup(config: Config, claude_projects: Path, now: float | None = None) -> dict:
    """Daily の材料と、テーマのディレクトリで動かした Claude のセッションの記録のうち、古いものを消す。"""
    now = time.time() if now is None else now
    m = config.maintenance
    root = config.research_root

    digests = list((root / OVERVIEW_DIR / ".ezra" / "digest").glob("*.md"))
    removed_digests = remove_older_than(digests, now - m.digest_retention_days * 86400)

    # 消すのは ~/research の下のディレクトリに対応するセッションだけ。ほかのプロジェクトには触らない
    workspaces = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")] if root.is_dir() else []
    names = {claude_project_dir_name(p.resolve()) for p in workspaces}
    sessions: list[Path] = []
    for name in names:
        project = claude_projects / name
        if project.is_dir():
            sessions += list(project.glob("*.jsonl"))
    removed_sessions = remove_older_than(sessions, now - m.session_retention_days * 86400)
    return {"digests": removed_digests, "sessions": removed_sessions}


def dump_state(config: Config) -> Path:
    """Ezra の状態を、差分の読みやすい形で ~/research/_ezra_state/ に書き出す。"""
    out = config.research_root / STATE_DIR
    out.mkdir(parents=True, exist_ok=True)
    if config.db_path.exists():
        src = sqlite3.connect(config.db_path)
        try:
            lines = "\n".join(src.iterdump())
        finally:
            src.close()
        (out / "ezra.sql").write_text(lines + "\n", encoding="utf-8")
    notion_state = config.state_dir / "notion.json"
    if notion_state.exists():
        shutil.copyfile(notion_state, out / "notion.json")
    return out


def exclude_large_files(repo: Path, limit: int = MAX_FILE_BYTES) -> list[str]:
    """大きすぎるファイルを .git/info/exclude に書き、コミットに入らないようにする。"""
    large = sorted(
        str(p.relative_to(repo)) for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(repo).parts and p.stat().st_size > limit
    )
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    text = re.sub(rf"\n?{re.escape(_EXCLUDE_BEGIN)}.*?{re.escape(_EXCLUDE_END)}\n?", "\n", text, flags=re.DOTALL)
    if large:
        text = text.rstrip("\n") + f"\n{_EXCLUDE_BEGIN}\n" + "\n".join(f"/{p}" for p in large) + f"\n{_EXCLUDE_END}\n"
    exclude.write_text(text.lstrip("\n"), encoding="utf-8")
    return large


async def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace").strip()


class BackupError(RuntimeError):
    pass


async def backup(config: Config, day: str | None = None) -> dict:
    repo = config.research_root
    if not (repo / ".git").is_dir():
        raise BackupError(f"{repo} が Git のリポジトリではありません（deploy/backup-init.sh を実行してください）")
    await asyncio.to_thread(dump_state, config)
    large = await asyncio.to_thread(exclude_large_files, repo)

    code, out = await _git(repo, "add", "-A")
    if code:
        raise BackupError(f"git add: {out}")
    code, _ = await _git(repo, "diff", "--cached", "--quiet")
    committed = False
    if code == 1:
        d = date.fromisoformat(day) if day else date.today()
        code, out = await _git(repo, "commit", "-q", "-m", f"{d.month}/{d.day} の研究データを保存する")
        if code:
            raise BackupError(f"git commit: {out}")
        committed = True
    code, out = await _git(repo, "push", "-q")
    if code:
        raise BackupError(f"git push: {out}")
    return {"committed": committed, "skipped_large_files": large}
