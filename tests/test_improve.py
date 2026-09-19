"""Slack から Ezra 自身を直す流れ（docs/plan.md の12章）。"""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from ezra import guard, improve, runner
from ezra.assistant import Assistant
from ezra.jobs import JobManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Ezra のリポジトリに見立てた git リポジトリ（origin は同じ tmp の中のベアリポジトリ）。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "ezra@example.com")
    git(path, "config", "user.name", "Ezra")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("x = 1\n")
    (path / "config.toml").write_text("research_root = \"~/research\"\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "はじめ")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "-q", "origin", "main")
    return path


@pytest.fixture
def env(config, store, repo, monkeypatch):
    config = replace(config, repo_root=repo)
    slack = FakeSlack({"C9": "research-ezra", "C1": "vlm"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_claude", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    return assistant, slack, claude, config


async def settle(assistant):
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks))


def edits_code(text="y = 2\n"):
    def side_effect(cwd: Path):
        (cwd / "src" / "app.py").write_text(text)
    return side_effect


async def agreed(assistant, slack, claude, request="直して"):
    """依頼 → 案 →「いいよ」→「これで進めていい？」まで進めたスレッドにする。"""
    claude.behaviors = [{"text": "こう直すつもり"}, {"text": "これで進めていい？"}]
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": f"<@UBOT> {request}"})
    await settle(assistant)
    slack.replies = [{"user": "UME", "ts": "20.1", "text": request},
                     {"user": "UBOT", "bot_id": "B1", "ts": "20.2", "text": "こう直すつもり"}]
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.3", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)
    slack.replies += [{"user": "UME", "ts": "20.3", "text": "いいよ"},
                      {"user": "UBOT", "bot_id": "B1", "ts": "20.4", "text": "これで進めていい？"}]


async def second_yes(assistant, ts="20.5"):
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": ts, "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)


# 差分の確認（柵）

def test_check_change_rejects_protected_paths(repo):
    git(repo, "checkout", "-q", "-b", "work")
    (repo / "config.toml").write_text("research_root = \"/tmp\"\n")
    git(repo, "commit", "-qam", "柵を触る")
    problems = guard.check_change(repo, "main", "HEAD")
    assert any("柵のファイル" in p for p in problems)


def test_check_change_rejects_secrets(repo):
    git(repo, "checkout", "-q", "-b", "work")
    (repo / "src" / "app.py").write_text('TOKEN = "xoxb-1234567890-abcdefghij"\n')
    git(repo, "commit", "-qam", "鍵を書く")
    problems = guard.check_change(repo, "main", "HEAD")
    assert any("秘密情報" in p for p in problems), problems


def test_check_change_accepts_a_normal_fix(repo):
    git(repo, "checkout", "-q", "-b", "work")
    (repo / "src" / "app.py").write_text("x = 2\n")
    git(repo, "commit", "-qam", "直す")
    assert guard.check_change(repo, "main", "HEAD") == []


def test_commit_message_uses_the_subject_claude_wrote():
    summary = "**やったこと**\n\nログの進捗行を間引いた。\n\n📝 件名: ジョブのログから進捗の行を間引く"
    message = improve.commit_message("ログが読みにくい（とても長い要望の文が続く）", summary)
    assert message.splitlines()[0] == "ジョブのログから進捗の行を間引く"
    assert "件名:" not in message and "ログの進捗行を間引いた。" in message
    assert message.splitlines()[1] == "" and "**やったこと**" in message   # 空行を残す


def test_commit_message_falls_back_to_the_request():
    message = improve.commit_message("ログが読みにくい", "直したよ")
    assert message.splitlines()[0] == "ログが読みにくい"


# やりとりから着手まで

async def test_improve_channel_records_backlog_and_plans_without_writing_code(env, config):
    assistant, slack, claude, cfg = env
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    backlog = cfg.backlog_path.read_text()
    assert "#research-ezra" in backlog and "経過をもっと細かく" in backlog
    call, = claude.calls
    # 書けるのは一時ディレクトリだけ。読めるのは Ezra のリポジトリ
    assert call["cwd"] == cfg.state_dir / "improve" / "20.1" and call["cwd"].is_dir()
    from ezra.themes import ChannelKind, Workspace
    settings_json = guard.build_settings(cfg, Workspace("research-ezra", ChannelKind.IMPROVE, call["cwd"]))
    allow = settings_json["permissions"]["allow"]
    assert f"Read(/{cfg.repo_root}/**)" in allow and f"Edit(/{call['cwd']}/**)" in allow
    assert f"Edit(/{cfg.repo_root}/**)" not in allow


async def test_start_marker_creates_a_worktree_and_reports_the_change(env):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [
        {"text": "じゃあやるね\n🛠 着手"},
        {"text": "直したよ。テストは通った\n📝 件名: app.py の値を直す", "side_effect": edits_code()},
    ]
    await second_yes(assistant)

    row = assistant.store.improvement("C9", "20.1")
    assert row["status"] == "review" and row["branch"] == "ezra/improve-20-1"
    assert claude.calls[-1]["cwd"] == Path(row["worktree"])   # 最後の回が worktree での直し
    assert git(Path(row["worktree"]), "log", "-1", "--format=%s") == "app.py の値を直す"
    texts = "\n".join(slack.texts())
    assert "直し始めるね" in texts and "取り込んでいい？" in texts and "`src/app.py`" in texts
    upload, = [kw for name, kw in slack.calls if name == "files_upload_v2"]
    assert upload["file_uploads"][0]["filename"] == "change.diff"


async def test_start_marker_from_an_automatic_run_is_ignored(env):
    """ジョブの完了などで自動で再開した回の返事に着手の行があっても、動かない。"""
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}]
    from ezra.assistant import Request
    await assistant.submit(Request("C9", "research-ezra", "20.1", None, "ジョブが終わった", trigger="job"))
    await settle(assistant)
    assert assistant.store.improvement("C9", "20.1") is None


async def test_only_one_improvement_at_a_time(env):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude, "1つめ")
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直した", "side_effect": edits_code()}, {"text": "🛠 着手"}]
    await second_yes(assistant)
    claude.behaviors = [{"text": "案だよ"}, {"text": "🛠 着手"}]
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "21.1", "text": "<@UBOT> 2つめ"})
    await settle(assistant)
    slack.replies = [{"user": "UME", "ts": "21.1", "text": "2つめ"},
                     {"user": "UME", "ts": "21.2", "text": "いいよ"},
                     {"user": "UBOT", "bot_id": "B1", "ts": "21.3", "text": "これで進めていい？"}]
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "21.4", "thread_ts": "21.1", "text": "いいよ"})
    await settle(assistant)
    assert assistant.store.improvement("C9", "21.1") is None
    assert "先に進んでいる直しがある" in "\n".join(slack.texts())


# 取り込みと再起動

async def prepared(env, monkeypatch):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直したよ", "side_effect": edits_code()}]
    await second_yes(assistant)
    monkeypatch.setattr(improve, "run_checks", lambda worktree: improve.CommandResult(True, "テストは通った"))
    claude.behaviors = [{"text": "じゃあ入れるね\n📦 取り込み"}]
    return assistant, slack, claude, cfg


async def test_merge_marker_merges_pushes_and_asks_for_a_restart(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    row = assistant.store.improvement("C9", "20.1")
    assert row["status"] == "restarting"
    assert (cfg.repo_root / "src" / "app.py").read_text() == "y = 2\n"           # main に入った
    assert git(cfg.repo_root, "rev-parse", "main") == git(cfg.repo_root, "rev-parse", "origin/main")  # push した
    assert improve.read_pending(cfg) == (row["base_commit"], "20.1")             # 戻せるようにしてある
    await asyncio.wait_for(assistant.restart_requested.wait(), 1)                 # 作業がないので終了へ


async def test_merge_stops_when_the_repository_has_uncommitted_changes(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    (cfg.repo_root / "src" / "app.py").write_text("人の書きかけ\n")

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    assert assistant.store.improvement("C9", "20.1")["status"] == "review"
    assert "コミットしていない変更がある" in "\n".join(slack.texts())
    assert (cfg.repo_root / "src" / "app.py").read_text() == "人の書きかけ\n"


async def test_merge_catches_up_when_main_moved_and_asks_again(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    (cfg.repo_root / "README.md").write_text("先に進んだ\n")
    git(cfg.repo_root, "add", "-A")
    git(cfg.repo_root, "commit", "-qm", "人が先に進めた")

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    row = assistant.store.improvement("C9", "20.1")
    assert row["status"] == "review" and row["base_commit"] == git(cfg.repo_root, "rev-parse", "HEAD")
    assert "乗せ直した" in "\n".join(slack.texts())


async def test_failed_checks_are_reported_and_nothing_is_merged(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    monkeypatch.setattr(improve, "run_checks", lambda worktree: improve.CommandResult(False, "1 failed"))
    before = git(cfg.repo_root, "rev-parse", "main")

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    assert git(cfg.repo_root, "rev-parse", "main") == before
    assert "確認が通らなかった" in "\n".join(slack.texts())


async def test_protected_change_is_not_offered_for_review(env):
    assistant, slack, claude, cfg = env

    def touches_guard(cwd: Path):
        (cwd / "config.toml").write_text("research_root = \"/tmp\"\n")

    await agreed(assistant, slack, claude, "柵を変えて")
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直した", "side_effect": touches_guard}]
    await second_yes(assistant)

    assert assistant.store.improvement("C9", "20.1")["status"] == "failed"
    assert "柵のファイルに触れています" in "\n".join(slack.texts())


# 起動したときの知らせ

async def test_announce_after_a_successful_update(env):
    assistant, slack, claude, cfg = env
    assistant.store.start_improvement("C9", "20.1", "経過を細かく", status="restarting", merge_commit="abcdef1234")
    assistant.store.update_improvement("C9", "20.1", status="restarting", merge_commit="abcdef1234")
    improve.mark_pending(cfg, "0123456789", "20.1")

    await assistant.announce_update()

    assert assistant.store.improvement("C9", "20.1")["status"] == "done"
    assert not improve.pending_path(cfg).exists()
    assert "新しい版で起動したよ" in "\n".join(slack.texts())


async def test_announce_after_a_rollback(env, monkeypatch):
    assistant, slack, claude, cfg = env
    assistant.store.start_improvement("C9", "20.1", "経過を細かく", status="restarting")
    monkeypatch.setattr(improve, "push_revert", lambda config: None)
    improve.rolled_back_path(cfg).write_text("0123456789\n4\n20.1\n")

    await assistant.announce_update()

    assert assistant.store.improvement("C9", "20.1")["status"] == "failed"
    assert not improve.rolled_back_path(cfg).exists()
    assert "起動できなかったので" in "\n".join(slack.texts())


async def test_start_needs_a_second_yes(env):
    """案への「いいよ」だけでは着手しない。「これで進めていい？」にもう一度答えてから。"""
    assistant, slack, claude, cfg = env
    claude.behaviors = [{"text": "こう直すつもり"}, {"text": "じゃあやるね\n🛠 着手"}]
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 直して"})
    await settle(assistant)
    slack.replies = [{"user": "UME", "ts": "20.1", "text": "直して"},
                     {"user": "UBOT", "bot_id": "B1", "ts": "20.2", "text": "こう直すつもり"}]

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.3", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)
    assert assistant.store.improvement("C9", "20.1") is None      # 1回目では着手しない
    assert "この直し方で進めていい？" in "\n".join(slack.texts())

    slack.replies += [{"user": "UME", "ts": "20.3", "text": "いいよ"},
                      {"user": "UBOT", "bot_id": "B1", "ts": "20.4", "text": "これで進めていい？"}]
    claude.behaviors = [{"text": "じゃあやるね\n🛠 着手"}, {"text": "直した", "side_effect": edits_code()}]
    await second_yes(assistant)
    assert assistant.store.improvement("C9", "20.1")["status"] == "review"


def test_strip_markers_removes_only_marker_lines():
    text = "直したよ。\n:memo: 件名: x\n🛠 着手\n📦 取り込み"
    assert improve.strip_markers(text) == "直したよ。\n:memo: 件名: x"
