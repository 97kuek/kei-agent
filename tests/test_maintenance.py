import asyncio
import json
import logging
import os
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from kei_agent import maintenance, themes
from kei_agent.app import setup_logging


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_cleanup_removes_only_old_files_of_research_dirs(config, tmp_path):
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)

    projects = tmp_path / "claude-projects"
    theme_project = projects / maintenance.claude_project_dir_name(ws.cwd.resolve())
    other_project = projects / "-Users-someone-other-project"
    for d in (theme_project, other_project):
        d.mkdir(parents=True)
        (d / "old.jsonl").write_text("{}")
        age(d / "old.jsonl", 100)
    (theme_project / "new.jsonl").write_text("{}")

    removed = maintenance.cleanup(config, projects)

    assert removed == {"sessions": 1, "thread_logs": 0}
    assert not (theme_project / "old.jsonl").exists() and (theme_project / "new.jsonl").exists()
    assert (other_project / "old.jsonl").exists()  # ほかのプロジェクトには触らない
    # Claude Code の作るディレクトリ名と同じ（_ も - になる）
    assert maintenance.claude_project_dir_name(Path("/Users/k/research/_overview")) == "-Users-k-research--overview"


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


def pushed_repo(path, remote, files):
    """files を1回コミットして、裸の remote に push 済みの Git リポジトリ。"""
    git(remote.parent, "init", "-q", "--bare", "-b", "main", str(remote))
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "remote", "add", "origin", str(remote))
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    git(path, "push", "-q", "-u", "origin", "main")
    return remote


async def test_backup_commits_and_pushes(config, store, tmp_path):
    root = config.research_root
    remote = pushed_repo(root, tmp_path / "remote.git", {"vlm/CLAUDE.md": "# vlm"})

    (root / "vlm" / "result.csv").write_text("a,b\n")
    detail = await maintenance.backup(config, "2026-09-18")

    assert detail["committed"] is True
    log = git(remote, "log", "--format=%s", "main")
    assert log.splitlines()[0] == "9/18 の研究データを保存する"
    files = git(remote, "ls-tree", "-r", "--name-only", "main")
    # 状態の書き出しは Kei Agent 側に移したので、研究のリポジトリには入らない
    assert "vlm/result.csv" in files and "kei-agent.sql" not in files
    # Kei Agent 側が Git になっていないことは、黙って見逃さずに結果へ出す
    assert detail["agent_root"] == {"status": "not_a_repo", "path": str(config.agent_root)}

    again = await maintenance.backup(config, "2026-09-18")
    assert again["committed"] is False


async def test_backup_requires_repository(config):
    config.research_root.mkdir(parents=True)
    with pytest.raises(maintenance.BackupError):
        await maintenance.backup(config)


def test_setup_logging_rotates_file(tmp_path):
    path = tmp_path / "logs" / "kei-agent.log"
    setup_logging({"KEI_AGENT_LOG_FILE": str(path)})
    try:
        handler, = logging.getLogger().handlers
        assert isinstance(handler, RotatingFileHandler) and handler.maxBytes == 5 * 1024 * 1024
        logging.getLogger("kei_agent").info("こんにちは")
        handler.flush()
        assert "こんにちは" in path.read_text()
    finally:
        logging.basicConfig(force=True, handlers=[logging.NullHandler()])


def test_repeated_slack_reconnect_failures_are_thinned_out():
    """ネットが切れている間、Slack の接続は数秒ごとに失敗を書く。同じものは10分に1行だけ残し、省いた数を添える。"""
    from kei_agent.app import RepeatFilter

    kept = []
    thin = RepeatFilter(("Failed to check the current session",), seconds=600)

    def log(message: str, at: float) -> None:
        record = logging.LogRecord("slack_bolt.AsyncApp", logging.ERROR, __file__, 1, message, None, None)
        record.created = at
        if thin.filter(record):
            kept.append(record.getMessage())

    for at in range(0, 900, 10):
        log("Failed to check the current session (s_1) or reconnect to the server (error: DNS)", at)
    log("A new session (s_2) has been established", 901)
    assert kept == ["Failed to check the current session (s_1) or reconnect to the server (error: DNS)",
                    "Failed to check the current session (s_1) or reconnect to the server (error: DNS)"
                    "（同じ失敗 59 件は省いた）",
                    "A new session (s_2) has been established"]


async def test_backup_untracks_a_file_that_grew_too_large(config, monkeypatch):
    """小さいうちにコミットしたファイルが育つと、exclude では止まらず push が通らなくなる。"""
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
    repo = config.research_root
    repo.mkdir(parents=True, exist_ok=True)
    real = asyncio.create_subprocess_exec

    async def never_finishes(_program, *_args, **kw):
        return await real("sleep", "5", **{k: v for k, v in kw.items() if k in ("cwd", "stdout", "stderr", "env")})

    monkeypatch.setattr(maintenance.asyncio, "create_subprocess_exec", never_finishes)
    with pytest.raises(maintenance.BackupError, match="終わりませんでした"):
        await maintenance._git(repo, "push", "-q", timeout=0.3)


async def test_dump_state_writes_sql_and_notion_ids_from_another_thread(config, store):
    """毎晩の保守は別スレッドから呼ぶ。接続を作ったスレッド以外でも、SQL と Notion の ID を書き出せる。"""
    store.upsert_thread("C1", "10.1", "vlm", "sess-1")
    (config.state_dir / "notion.json").write_text(json.dumps({"home_page_id": "p"}))
    out = await asyncio.to_thread(maintenance.dump_state, config, store)
    sql = (out / "kei-agent.sql").read_text()
    assert "CREATE TABLE" in sql and 'INSERT INTO "threads"' in sql and "sess-1" in sql
    assert json.loads((out / "notion.json").read_text()) == {"home_page_id": "p"}


async def test_backup_saves_the_agent_side_too(config, store, tmp_path):
    """Kei Agent 自身のもの（overview の作業場と状態）は ~/research の外にあるので、別のリポジトリに保存する。"""
    research_remote = pushed_repo(config.research_root, tmp_path / "research.git", {".keep": ""})
    agent_remote = pushed_repo(config.agent_root, tmp_path / "agent.git", {".keep": ""})
    config.overview_dir.mkdir(parents=True)
    (config.overview_dir / "CLAUDE.md").write_text("# 研究全体")

    detail = await maintenance.backup(config, "2026-09-18")

    assert detail["agent_root"]["committed"] is True
    agent_files = git(agent_remote, "ls-tree", "-r", "--name-only", "main")
    assert "overview/CLAUDE.md" in agent_files
    assert "state/kei-agent.sql" in agent_files          # 状態の書き出しもこちら側
    research_files = git(research_remote, "ls-tree", "-r", "--name-only", "main")
    assert "kei-agent.sql" not in research_files
    assert git(agent_remote, "log", "--format=%s", "main").splitlines()[0] == \
        "9/18 の Kei Agent のデータを保存する"


async def test_agent_root_without_a_remote_is_reported_not_raised(config, store, tmp_path):
    """push 先が無くても保守そのものは止めない。ただし理由は必ず返す。"""
    pushed_repo(config.research_root, tmp_path / "research.git", {"vlm/CLAUDE.md": "# vlm"})
    # Kei Agent 側は Git にしたが、origin をまだ登録していない
    config.agent_root.mkdir(parents=True, exist_ok=True)
    git(config.agent_root, "init", "-q", "-b", "main")

    detail = await maintenance.backup(config, "2026-09-18")

    assert detail["agent_root"] == {"status": "no_remote", "path": str(config.agent_root)}
