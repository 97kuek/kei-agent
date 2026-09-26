"""Slack から Kei Agent 自身を直す流れ（docs/architecture.md）。"""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import guard, improve, issues, runner
from kei_agent.assistant import Assistant
from kei_agent.jobs import JobManager
from kei_agent.request import Request


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Kei Agent のリポジトリに見立てた git リポジトリ（origin は同じ tmp の中のベアリポジトリ）。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "kei-agent@example.com")
    git(path, "config", "user.name", "Kei Agent")
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
    slack = FakeSlack({"C9": "00_kei-agent", "C1": "vlm"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
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

async def test_new_request_becomes_a_public_issue_and_plans_without_writing_code(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env
    seen = []

    async def summarize(config, store, text):
        seen.append(text)
        return issues.Summary("作業中の経過を細かく見せる", "- 作業中の様子を短く出す")

    monkeypatch.setattr(issues, "summarize", summarize)
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    # 公開の issue には要約だけを載せる。原文は Slack に残し、ファイルにも書かない
    assert seen == ["経過をもっと細かく"]
    assert fake_github.created() == [{"title": "作業中の経過を細かく見せる", "body": "- 作業中の様子を短く出す",
                                      "label": "kei-agent-request"}]
    assert not any("経過をもっと細かく" in arg for args in fake_github.calls for arg in args)
    row = assistant.store.improvement("C9", "20.1")
    assert (row["status"], row["issue_number"], row["request"]) == ("planning", 1, "経過をもっと細かく")
    assert "<https://github.com/97kuek/kei-agent/issues/1|#1>" in "\n".join(slack.texts())
    assert not (cfg.overview_dir / "backlog.md").exists()
    call, = claude.calls
    # 書けるのは一時ディレクトリだけ。読めるのは Kei Agent のリポジトリ
    assert call["cwd"] == cfg.state_dir / "improve" / "20.1" and call["cwd"].is_dir()
    from kei_agent.themes import ChannelKind, Workspace
    settings_json = guard.build_settings(cfg, Workspace("research-agent", ChannelKind.IMPROVE, call["cwd"]))
    allow = settings_json["permissions"]["allow"]
    assert f"Read(/{cfg.repo_root}/**)" in allow and f"Edit(/{call['cwd']}/**)" in allow
    assert f"Edit(/{cfg.repo_root}/**)" not in allow


async def test_a_retried_request_does_not_open_a_second_issue(env, fake_github):
    """上限や再起動で止まった依頼をやり直しても、issue は1つのまま。"""
    assistant, slack, claude, cfg = env
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    await assistant.submit(Request("C9", "kei-agent", "20.1", "20.1", "経過をもっと細かく"))
    await settle(assistant)

    assert len(fake_github.created()) == 1
    assert assistant.store.improvement("C9", "20.1")["issue_number"] == 1


def trouble_notices(slack) -> list[str]:
    """改善チャンネルに、スレッドの外で出した知らせ。"""
    return [kw["text"] for kw in slack.posted() if kw["channel"] == "C9" and "thread_ts" not in kw]


async def test_request_stays_in_slack_when_gh_fails(env, fake_github):
    assistant, slack, claude, cfg = env
    fake_github.fail["issue create"] = issues.IssueError("gh が失敗しました", "HTTP 401: Bad credentials")

    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    assert assistant.store.improvement("C9", "20.1")["issue_number"] is None
    notice, = trouble_notices(slack)
    assert "GitHub の issue にできませんでした（gh が失敗しました）" in notice
    assert "Bad credentials" not in "\n".join(slack.texts())      # 詳しい中身はログにだけ残す
    assert not (cfg.overview_dir / "backlog.md").exists()          # ファイルには逃がさない
    assert len(claude.calls) == 1                                  # 案は考える


async def test_an_unexpected_error_while_filing_is_reported(env, fake_github):
    assistant, slack, claude, cfg = env
    fake_github.fail["issue create"] = RuntimeError("壊れた")

    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    notice, = trouble_notices(slack)
    assert "GitHub の issue にできませんでした" in notice
    assert len(claude.calls) == 1


async def test_an_unsafe_summary_opens_no_issue(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env

    async def summarize(config, store, text):
        raise issues.IssueError("要約が公開の条件に合いません", "URL を含む")

    monkeypatch.setattr(issues, "summarize", summarize)
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    assert fake_github.calls == []
    notice, = trouble_notices(slack)
    assert "要約が公開の条件に合いません" in notice


async def test_without_a_self_fix_provider_it_says_why_no_issue_was_made(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env

    async def summarize(config, store, text):
        raise issues.NoProvider("自己改善の AI（Claude か Codex）が選ばれていません")

    monkeypatch.setattr(issues, "summarize", summarize)
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    assert fake_github.calls == []
    thread = [kw["text"] for kw in slack.posted() if kw.get("thread_ts") == "20.1"]
    assert any("選ばれていない" in text and "issue にしなかった" in text for text in thread)


async def test_a_request_without_words_opens_no_issue(env, fake_github):
    assistant, slack, claude, cfg = env
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT>"})
    await settle(assistant)

    assert fake_github.calls == []
    assert "issue にはしなかった" in "\n".join(slack.texts())


async def test_improve_channel_intro_says_requests_become_public_issues(env):
    assistant, slack, claude, cfg = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C9"})
    intro = slack.texts()[-1]
    assert "公開の GitHub issue" in intro and "backlog" not in intro


async def test_start_marker_creates_a_worktree_and_reports_the_change(env):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [
        {"text": "じゃあやるね\n🛠 着手"},
        {"text": "直したよ。テストは通った\n📝 件名: app.py の値を直す", "side_effect": edits_code()},
    ]
    await second_yes(assistant)

    row = assistant.store.improvement("C9", "20.1")
    assert row["status"] == "review" and row["branch"] == "kei-agent/improve-20-1"
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
    await assistant.submit(Request("C9", "00_kei-agent", "20.1", None, "ジョブが終わった", trigger="job"))
    await settle(assistant)
    assert assistant.store.improvement("C9", "20.1")["status"] == "planning"   # 案のまま


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
    assert assistant.store.improvement("C9", "21.1")["status"] == "planning"
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
    assert row["status"] == "restarting" and row["issue_number"] == 1             # 要望の issue を覚えたまま
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

async def test_announce_after_a_successful_update(env, fake_github):
    assistant, slack, claude, cfg = env
    assistant.store.start_improvement("C9", "20.1", "経過を細かく", status="restarting", merge_commit="abcdef1234")
    assistant.store.update_improvement("C9", "20.1", status="restarting", merge_commit="abcdef1234")
    improve.mark_pending(cfg, "0123456789", "20.1")

    await assistant.announce_update()

    assert assistant.store.improvement("C9", "20.1")["status"] == "done"
    assert not improve.pending_path(cfg).exists()
    assert "新しい版で起動したよ" in "\n".join(slack.texts())
    assert fake_github.calls == []                    # issue にしていない要望は何もしない


def merged_request(assistant, cfg, issue_number=7):
    """issue にした要望を取り込み、新しい版で起動する直前の状態にする。"""
    assistant.store.request_improvement("C9", "20.1", "経過を細かく", issue_number)
    assistant.store.start_improvement("C9", "20.1", "いいよ", status="restarting", merge_commit="abcdef1234")
    improve.mark_pending(cfg, "0123456789", "20.1")


async def test_announce_closes_the_issue_of_the_merged_request(env, fake_github):
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)

    await assistant.announce_update()

    assert fake_github.closed() == [("7", "abcdef1 で取り込みました。")]
    assert assistant.store.improvement("C9", "20.1")["status"] == "done"


async def test_announce_goes_on_when_the_issue_cannot_be_closed(env, fake_github):
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)
    fake_github.fail["issue close"] = issues.IssueError("gh が失敗しました", "HTTP 502")

    await assistant.announce_update()

    assert assistant.store.improvement("C9", "20.1")["status"] == "done"
    notice, = trouble_notices(slack)
    assert "issue #7 を閉じられませんでした" in notice


async def test_announce_survives_an_unexpected_error_while_closing(env, fake_github):
    """起動の途中で呼ばれるので、想定外の失敗でも止まらずに知らせる。"""
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)
    fake_github.fail["issue close"] = RuntimeError("壊れた")

    await assistant.announce_update()

    notice, = trouble_notices(slack)
    assert "issue #7 を閉じられませんでした" in notice


async def test_announce_after_a_rollback(env, monkeypatch, fake_github):
    assistant, slack, claude, cfg = env
    assistant.store.request_improvement("C9", "20.1", "経過を細かく", 7)
    assistant.store.start_improvement("C9", "20.1", "経過を細かく", status="restarting")
    monkeypatch.setattr(improve, "push_revert", lambda config: None)
    improve.rolled_back_path(cfg).write_text("0123456789\n4\n20.1\n")

    await assistant.announce_update()

    assert assistant.store.improvement("C9", "20.1")["status"] == "failed"
    assert not improve.rolled_back_path(cfg).exists()
    assert "起動できなかったので" in "\n".join(slack.texts())
    assert fake_github.closed() == []                 # 戻した要望の issue は開いたまま


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
    assert assistant.store.improvement("C9", "20.1")["status"] == "planning"   # 1回目では着手しない
    assert "この直し方で進めていい？" in "\n".join(slack.texts())

    slack.replies += [{"user": "UME", "ts": "20.3", "text": "いいよ"},
                      {"user": "UBOT", "bot_id": "B1", "ts": "20.4", "text": "これで進めていい？"}]
    claude.behaviors = [{"text": "じゃあやるね\n🛠 着手"}, {"text": "直した", "side_effect": edits_code()}]
    await second_yes(assistant)
    assert assistant.store.improvement("C9", "20.1")["status"] == "review"


def test_strip_markers_removes_only_marker_lines():
    text = "直したよ。\n:memo: 件名: x\n🛠 着手\n📦 取り込み"
    assert improve.strip_markers(text) == "直したよ。\n:memo: 件名: x"


# 途中で止まったとき

async def test_unexpected_error_while_fixing_marks_the_improvement_failed(env, monkeypatch):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直したよ", "side_effect": edits_code()}]

    def broken(worktree, message):
        raise RuntimeError("git が落ちた")
    monkeypatch.setattr(improve, "commit_all", broken)
    await second_yes(assistant)

    row = assistant.store.improvement("C9", "20.1")
    assert row["status"] == "failed" and "git が落ちた" in row["detail"]
    assert "直している途中で止まった" in "\n".join(slack.texts())


async def test_fix_left_working_by_a_restart_is_marked_interrupted(env):
    assistant, slack, claude, cfg = env
    assistant.store.start_improvement("C9", "20.1", "経過を細かく")

    assert await assistant.recover_interrupted_fixes() == 1

    row = assistant.store.improvement("C9", "20.1")
    assert row["status"] == "failed" and row["detail"] == "中断"
    assert "中断した" in "\n".join(slack.texts())


async def test_push_failure_undoes_the_local_merge_and_keeps_review(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    before = git(cfg.repo_root, "rev-parse", "main")
    git(cfg.repo_root, "remote", "set-url", "origin", str(cfg.repo_root.parent / "missing.git"))

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    assert git(cfg.repo_root, "rev-parse", "main") == before                    # 手元の main は元のまま
    assert assistant.store.improvement("C9", "20.1")["status"] == "review"    # もう一度「いいよ」でやり直せる
    assert not improve.pending_path(cfg).exists()
    assert not assistant.restart_requested.is_set()
    assert "push できなかった" in "\n".join(slack.texts())


# backlog.md から issue へ（一度だけ）

def write_backlog(config) -> Path:
    """record_backlog が書いていた形の backlog.md。"""
    path = config.overview_dir / "backlog.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "# Kei Agent への要望", "", "`#kei-agent` で受け付けた要望。新しいものが下。", "",
        "- [x] 2026-09-01 10:00 済んだもの （[Slack](https://example.slack.com/archives/C9/p1)）（Kei Agent が直して取り込み済み）",
        "",
        "- [ ] 2026-09-02 11:00 経過をもっと細かく",
        "  二行目も見る （[Slack](https://example.slack.com/archives/C9/p2)）",
        "",
        "- [ ] 2026-09-03 12:00 朝の予定を短く ",
        "",
    ]), encoding="utf-8")
    return path


def numbered_summaries(monkeypatch) -> list[str]:
    """要約の偽物。渡された要望を残し、何番目かを題にする。"""
    seen: list[str] = []

    async def summarize(config, store, text):
        seen.append(text)
        return issues.Summary(f"要望その{len(seen)}", "- 要約した本文")

    monkeypatch.setattr(issues, "summarize", summarize)
    return seen


def test_backlog_migration_dry_run_only_proposes(config, fake_github, monkeypatch):
    path = write_backlog(config)
    seen = numbered_summaries(monkeypatch)

    proposals = improve.migrate_backlog_to_issues(config)

    assert seen == ["経過をもっと細かく\n二行目も見る", "朝の予定を短く"]    # 済んだものと、日時・Slack のリンクは渡さない
    assert [(p["request"], p["title"], p["body"], p.get("number")) for p in proposals] == [
        ("経過をもっと細かく\n二行目も見る", "要望その1", "- 要約した本文", None),
        ("朝の予定を短く", "要望その2", "- 要約した本文", None)]
    assert fake_github.calls == [] and path.exists()


def test_backlog_migration_creates_the_reviewed_issues_once(config, fake_github, monkeypatch):
    path = write_backlog(config)
    seen = numbered_summaries(monkeypatch)
    improve.migrate_backlog_to_issues(config)                         # 要約を見て確かめてから

    created = improve.migrate_backlog_to_issues(config, dry_run=False)
    again = improve.migrate_backlog_to_issues(config, dry_run=False)

    assert len(seen) == 2                                             # 見た要約のまま作る（要約し直さない）
    assert [p["number"] for p in created] == [1, 2] == [p["number"] for p in again]
    assert [c["title"] for c in fake_github.created()] == ["要望その1", "要望その2"]   # 二度は作らない
    assert path.exists()                                              # 消すのは人


def test_backlog_migration_reports_what_it_could_not_file(config, fake_github, monkeypatch):
    write_backlog(config)

    async def summarize(config, store, text):
        if text.startswith("朝"):
            raise issues.IssueError("要約が公開の条件に合いません", "URL を含む")
        return issues.Summary("経過を細かく見せる", "- 要約した本文")

    monkeypatch.setattr(issues, "summarize", summarize)

    drafts = improve.migrate_backlog_to_issues(config)
    assert "要約が公開の条件に合いません" in drafts[1]["error"]

    results = improve.migrate_backlog_to_issues(config, dry_run=False)

    # 下書きで要約できなかったものは、本番の実行で要約し直して作らない（見て確かめていないので）
    assert [r.get("number") for r in results] == [1, None]
    assert "確かめた要約がありません" in results[1]["error"]
    assert [c["title"] for c in fake_github.created()] == ["経過を細かく見せる"]


def test_backlog_migration_files_nothing_without_reviewed_drafts(config, fake_github, monkeypatch):
    write_backlog(config)

    async def summarize(config, store, text):
        raise AssertionError("本番の実行では要約しない")

    monkeypatch.setattr(issues, "summarize", summarize)

    results = improve.migrate_backlog_to_issues(config, dry_run=False)

    assert all("確かめた要約がありません" in r["error"] for r in results)
    assert fake_github.created() == []


def test_backlog_migration_without_a_backlog(config, fake_github):
    assert improve.migrate_backlog_to_issues(config, dry_run=False) == []
    assert fake_github.calls == []
