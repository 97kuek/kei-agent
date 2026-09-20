"""毎晩の保守: 古いファイルの整理と、研究データのバックアップ。

バックアップは ~/research を Git のリポジトリとして扱い、変更をコミットして push する。
Kei Agent の状態（SQLite と Notion の ID）は、SQL のテキストなどにして ~/research/_kei_agent_state/ に書き出してから含める。
準備は deploy/backup-init.sh で行う。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import date
from pathlib import Path

from kei_agent.config import Config
from kei_agent.store import Store
from kei_agent.themes import OVERVIEW_DIR

STATE_DIR = "_kei_agent_state"
# GitHub が受け付けない大きさ。これより大きいファイルはコミットしない
MAX_FILE_BYTES = 50 * 1024 * 1024
_EXCLUDE_BEGIN = "# kei-agent: 大きすぎるファイル（自動で書き換える）"
_EXCLUDE_END = "# kei-agent: ここまで"
# git が終わらないときに諦めるまでの秒数（認証待ちで止まると、定期処理ごと止まる）
GIT_TIMEOUT_SECONDS = 300


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


def cleanup(config: Config, claude_projects: Path, now: float | None = None,
            keep_worktrees: frozenset[str] = frozenset(), keep_scratch: frozenset[str] = frozenset()) -> dict:
    """Daily の材料、Claude のセッションの記録、使い終わった worktree のうち、古いものを消す。"""
    now = time.time() if now is None else now
    m = config.maintenance
    root = config.research_root

    digests = list((root / OVERVIEW_DIR / ".kei-agent" / "digest").glob("*.md"))
    # 声の会話の全文（決まったことは Slack に残るので、控えは Daily の材料と同じ日数で消す）
    digests += list((root / OVERVIEW_DIR / "voice").glob("*.md"))
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

    # スレッドのログ（Codex が経緯を読むためのもの）も、セッションと同じ日数で整理する
    logs = [p for ws in workspaces for p in (ws / ".kei-agent" / "threads").glob("*.md")]
    removed_logs = remove_older_than(logs, now - m.thread_log_retention_days * 86400)
    removed_worktrees = remove_finished_worktrees(config, keep_worktrees, keep_scratch)
    return {"digests": removed_digests, "sessions": removed_sessions, "thread_logs": removed_logs,
            "worktrees": removed_worktrees}


def remove_finished_worktrees(config: Config, keep_worktrees: frozenset[str],
                              keep_scratch: frozenset[str]) -> int:
    """Kei Agent 自身を直すのに使った worktree と一時ディレクトリのうち、もう使っていないものを片付ける。

    いま使っているものの名前は、SQLite を持っている側（schedule.py）が渡す。
    """
    from kei_agent import improve

    in_use, active = keep_worktrees, keep_scratch
    removed = 0
    root = improve.worktree_root(config)
    if root.is_dir():
        for path in sorted(p for p in root.iterdir() if p.is_dir()):
            if path.name in in_use:
                continue
            improve.remove_worktree(config, path, f"kei-agent/{path.name}")
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    scratch = config.state_dir / "improve"
    if scratch.is_dir():
        for path in sorted(p for p in scratch.iterdir() if p.is_dir()):
            if path.name not in active:
                shutil.rmtree(path, ignore_errors=True)
    return removed


def dump_state(config: Config, store: Store | None = None) -> Path:
    """Kei Agent の状態を、差分の読みやすい形で ~/research/_kei_agent_state/ に書き出す。

    Kei Agent が動いている最中に読むので、いったんスナップショットを取ってから書き出す。
    直接 iterdump すると、ジョブの途中の状態が混ざる。
    """
    out = config.research_root / STATE_DIR
    out.mkdir(parents=True, exist_ok=True)
    if config.db_path.exists():
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "snapshot.db"
            if store is not None:
                store.snapshot(copy)
            else:
                Store(config.db_path).snapshot(copy)
            src = sqlite3.connect(copy)
            try:
                lines = "\n".join(src.iterdump())
            finally:
                src.close()
        (out / "kei-agent.sql").write_text(lines + "\n", encoding="utf-8")
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


class BackupError(RuntimeError):
    pass


async def _git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> tuple[int, str]:
    """git を1回動かす。認証を聞かれても、端末がないので待たずに失敗させる。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
             "GIT_CONFIG_PARAMETERS": "'credential.interactive=never'"},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackupError(f"git {' '.join(args)} が {int(timeout)} 秒で終わりませんでした") from None
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def backup(config: Config, day: str | None = None, store: Store | None = None) -> dict:
    repo = config.research_root
    if not (repo / ".git").is_dir():
        raise BackupError(f"{repo} が Git のリポジトリではありません（deploy/backup-init.sh を実行してください）")
    await asyncio.to_thread(dump_state, config, store)
    large = await asyncio.to_thread(exclude_large_files, repo)

    for path in large:
        # .git/info/exclude は「まだ追跡していない」ファイルにしか効かない。
        # 小さいうちにコミットしたファイルが育った場合は、追跡から外さないと push が通らない
        await _git(repo, "rm", "--cached", "-q", "--ignore-unmatch", "--", path)

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
