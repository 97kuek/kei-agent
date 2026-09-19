import json
import logging
import os
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from ezra import maintenance, themes
from ezra.app import setup_logging


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_cleanup_removes_only_old_files_of_research_dirs(config, tmp_path):
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)
    digest_dir = config.research_root / "_overview" / ".ezra" / "digest"
    digest_dir.mkdir(parents=True)
    (digest_dir / "old.md").write_text("x")
    (digest_dir / "new.md").write_text("x")
    age(digest_dir / "old.md", 40)

    projects = tmp_path / "claude-projects"
    theme_project = projects / maintenance.claude_project_dir_name(ws.cwd.resolve())
    other_project = projects / "-Users-someone-other-project"
    for d in (theme_project, other_project):
        d.mkdir(parents=True)
        (d / "old.jsonl").write_text("{}")
        age(d / "old.jsonl", 100)
    (theme_project / "new.jsonl").write_text("{}")

    removed = maintenance.cleanup(config, projects)

    assert removed == {"digests": 1, "sessions": 1, "thread_logs": 0, "worktrees": 0}
    assert not (digest_dir / "old.md").exists() and (digest_dir / "new.md").exists()
    assert not (theme_project / "old.jsonl").exists() and (theme_project / "new.jsonl").exists()
    assert (other_project / "old.jsonl").exists()  # ほかのプロジェクトには触らない


def test_claude_project_dir_name_matches_claude_code():
    from pathlib import Path
    assert maintenance.claude_project_dir_name(Path("/Users/k/research/_overview")) == "-Users-k-research--overview"


def test_dump_state_writes_sql_and_notion_ids(config, store):
    store.upsert_thread("C1", "1.1", "vlm", "sess")
    (config.state_dir / "notion.json").write_text(json.dumps({"home_page_id": "p"}))
    out = maintenance.dump_state(config)
    assert "INSERT INTO \"threads\"" in (out / "ezra.sql").read_text()
    assert json.loads((out / "notion.json").read_text()) == {"home_page_id": "p"}


def test_exclude_large_files_rewrites_its_own_block(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("# 手で書いた行\n")
    (tmp_path / "big.bin").write_bytes(b"x" * 20)
    (tmp_path / "small.txt").write_bytes(b"x")

    assert maintenance.exclude_large_files(tmp_path, limit=10) == ["big.bin"]
    (tmp_path / "big.bin").unlink()
    assert maintenance.exclude_large_files(tmp_path, limit=10) == []

    text = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert "# 手で書いた行" in text and "big.bin" not in text


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


async def test_backup_commits_and_pushes(config, store, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    root = config.research_root
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "remote", "add", "origin", str(remote))
    (root / "vlm").mkdir()
    (root / "vlm" / "CLAUDE.md").write_text("# vlm")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "push", "-q", "-u", "origin", "main")

    (root / "vlm" / "result.csv").write_text("a,b\n")
    detail = await maintenance.backup(config, "2026-09-18")

    assert detail["committed"] is True
    log = git(remote, "log", "--format=%s", "main")
    assert log.splitlines()[0] == "9/18 の研究データを保存する"
    files = git(remote, "ls-tree", "-r", "--name-only", "main")
    assert "vlm/result.csv" in files and "_ezra_state/ezra.sql" in files

    again = await maintenance.backup(config, "2026-09-18")
    assert again["committed"] is False


async def test_backup_requires_repository(config):
    config.research_root.mkdir(parents=True)
    with pytest.raises(maintenance.BackupError):
        await maintenance.backup(config)


def test_setup_logging_rotates_file(tmp_path):
    path = tmp_path / "logs" / "ezra.log"
    setup_logging({"EZRA_LOG_FILE": str(path)})
    try:
        handler, = logging.getLogger().handlers
        assert isinstance(handler, RotatingFileHandler) and handler.maxBytes == 5 * 1024 * 1024
        logging.getLogger("ezra").info("こんにちは")
        handler.flush()
        assert "こんにちは" in path.read_text()
    finally:
        logging.basicConfig(force=True, handlers=[logging.NullHandler()])


async def test_backup_untracks_a_file_that_grew_too_large(config, monkeypatch):
    """小さいうちにコミットしたファイルが育つと、exclude では止まらず push が通らなくなる。"""
    from ezra import maintenance

    repo = config.research_root
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    calls = []

    async def fake_git(_repo, *args, **kw):
        calls.append(args)
        return (1 if args[:2] == ("diff", "--cached") else 0), ""

    monkeypatch.setattr(maintenance, "_git", fake_git)
    monkeypatch.setattr(maintenance, "dump_state", lambda *a: repo)
    monkeypatch.setattr(maintenance, "exclude_large_files", lambda _repo: ["outputs/model.pt"])

    result = await maintenance.backup(config, "2026-09-19")

    untrack = ("rm", "--cached", "-q", "--ignore-unmatch", "--", "outputs/model.pt")
    assert untrack in calls and calls.index(untrack) < calls.index(("add", "-A"))
    assert result["skipped_large_files"] == ["outputs/model.pt"]


async def test_git_gives_up_instead_of_waiting_forever(config, monkeypatch):
    """端末のない launchd では、認証を聞かれると永久に止まり、定期処理ごと動かなくなる。"""
    import asyncio

    from ezra import maintenance

    repo = config.research_root
    repo.mkdir(parents=True, exist_ok=True)
    real = asyncio.create_subprocess_exec

    async def never_finishes(_program, *_args, **kw):
        return await real("sleep", "5", **{k: v for k, v in kw.items() if k in ("cwd", "stdout", "stderr", "env")})

    monkeypatch.setattr(maintenance.asyncio, "create_subprocess_exec", never_finishes)
    with pytest.raises(maintenance.BackupError, match="終わりませんでした"):
        await maintenance._git(repo, "push", "-q", timeout=0.3)


def test_cleanup_removes_leftover_worktrees_and_scratch(config, store, tmp_path, monkeypatch):
    """取り込みや失敗で使い終わった worktree と、案を考えるときの一時ディレクトリを片付ける。"""
    from ezra import improve

    store.start_improvement("C9", "20.1", "進行中", worktree=str(config.state_dir / "worktrees" / "improve-20-1"))
    leftovers = []

    def fake_remove(cfg, path, branch):
        leftovers.append((Path(path).name, branch))

    monkeypatch.setattr(improve, "remove_worktree", fake_remove)
    worktrees = improve.worktree_root(config)
    worktrees.mkdir(parents=True)
    (worktrees / "improve-20-1").mkdir()      # まだ使っている
    (worktrees / "improve-19-9").mkdir()      # もう使っていない
    scratch = config.state_dir / "improve"
    (scratch / "19.9").mkdir(parents=True)
    (scratch / "20.1").mkdir(parents=True)
    monkeypatch.setattr(improve, "worktree_root", lambda cfg: worktrees)

    busy = store.improvements_in("working", "review", "restarting")
    removed = maintenance.cleanup(config, tmp_path / "projects", None,
                                  frozenset(Path(r["worktree"]).name for r in busy if r["worktree"]),
                                  frozenset(r["thread_ts"] for r in busy))

    assert leftovers == [("improve-19-9", "ezra/improve-19-9")]
    assert removed["worktrees"] == 1
    assert not (scratch / "19.9").exists() and (scratch / "20.1").exists()
