"""自己改善のモジュール（段階3の自己改善の②。modules/improve/）。Slack から Kei Agent 自身を直す流れ。

Kei Agent のチャンネルで要望を聞き、公開の issue にし、案を相談して、worktree で直し、確認してから main に取り込んで、
新しい版で起動し直す。起動したときに結果を知らせる。記録はモジュールの記録（本体の表からは一度だけ写す）。
"""

import asyncio
import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import guard, runner, updates
from kei_agent.assistant import Assistant
from kei_agent.config import AgentProfile
from kei_agent.jobs import JobManager
from kei_agent.request import Request
from kei_agent.slack_text import strip_lines
from kei_agent.store import Store
from kei_agent_modules.improve import issues
from kei_agent_modules.improve import repo as improve_repo
from kei_agent_modules.improve.module import HIDDEN


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
    (path / "config.example.toml").write_text("research_root = \"~/research\"\n")
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
        await asyncio.sleep(0)


def fix_of(assistant, thread_ts="20.1"):
    """自己改善のモジュールの、そのスレッドの記録。"""
    return assistant.modules["improve"].fixes.get(thread_ts)


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


# コミットの件名

def test_commit_message_uses_the_subject_claude_wrote():
    summary = "**やったこと**\n\nログの進捗行を間引いた。\n\n📝 件名: ジョブのログから進捗の行を間引く"
    message = improve_repo.commit_message("ログが読みにくい（とても長い要望の文が続く）", summary)
    assert message.splitlines()[0] == "ジョブのログから進捗の行を間引く"
    assert "件名:" not in message and "ログの進捗行を間引いた。" in message
    assert message.splitlines()[1] == "" and "**やったこと**" in message   # 空行を残す


def test_commit_message_falls_back_to_the_request():
    message = improve_repo.commit_message("ログが読みにくい", "直したよ")
    assert message.splitlines()[0] == "ログが読みにくい"


# やりとりから着手まで

async def test_new_request_becomes_a_public_issue_and_plans_without_writing_code(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env
    seen = []

    async def summarize(run_ai, text, has_secret):
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
    fix = fix_of(assistant)
    assert (fix.status, fix.issue_number, fix.request) == ("planning", 1, "経過をもっと細かく")
    assert "<https://github.com/97kuek/kei-agent/issues/1|#1>" in "\n".join(slack.texts())
    assert not (cfg.overview_dir / "backlog.md").exists()
    call, = claude.calls
    # 書けるのは相談の作業用のフォルダだけ。読めるのは Kei Agent のリポジトリ
    assert call["cwd"] == cfg.module_state("improve") / "talk" / "20.1" and call["cwd"].is_dir()
    from kei_agent.agent_policy import policy_of
    from kei_agent.themes import ChannelKind, Workspace
    settings_json = guard.build_settings(cfg, Workspace("research-agent", ChannelKind.IMPROVE, call["cwd"],
                                                        module="improve"), policy_of("improve"))
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
    assert fix_of(assistant).issue_number == 1


def trouble_notices(slack) -> list[str]:
    """改善チャンネルに、スレッドの外で出した知らせ。"""
    return [kw["text"] for kw in slack.posted() if kw["channel"] == "C9" and "thread_ts" not in kw]


async def test_request_stays_in_slack_when_gh_fails(env, fake_github):
    assistant, slack, claude, cfg = env
    fake_github.fail["issue create"] = issues.IssueError("gh が失敗しました", "HTTP 401: Bad credentials")

    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    assert fix_of(assistant).issue_number is None
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

    async def summarize(run_ai, text, has_secret):
        raise issues.IssueError("要約が公開の条件に合いません", "URL を含む")

    monkeypatch.setattr(issues, "summarize", summarize)
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> 経過をもっと細かく"})
    await settle(assistant)

    assert fake_github.calls == []
    notice, = trouble_notices(slack)
    assert "要約が公開の条件に合いません" in notice


async def test_without_a_provider_it_says_why_no_issue_was_made(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env
    config = replace(cfg, agent_profiles={**cfg.agent_profiles, "improve": AgentProfile()})
    object.__setattr__(assistant, "config", config)
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

    fix = fix_of(assistant)
    assert fix.status == "review" and fix.branch == "kei-agent/improve-20-1"
    assert Path(fix.worktree).parent == cfg.module_state("improve") / "worktrees"
    assert claude.calls[-1]["cwd"] == Path(fix.worktree).resolve()   # 最後の回が worktree での直し
    assert git(Path(fix.worktree), "log", "-1", "--format=%s") == "app.py の値を直す"
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
    assert fix_of(assistant).status == "planning"   # 案のまま


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
    assert fix_of(assistant, "21.1").status == "planning"
    assert "先に進んでいる直しがある" in "\n".join(slack.texts())


# 取り込みと再起動

async def prepared(env, monkeypatch):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直したよ", "side_effect": edits_code()}]
    await second_yes(assistant)
    monkeypatch.setattr(improve_repo, "run_checks", lambda worktree: improve_repo.CommandResult(True, "テストは通った"))
    claude.behaviors = [{"text": "じゃあ入れるね\n📦 取り込み"}]
    return assistant, slack, claude, cfg


async def test_merge_marker_merges_pushes_and_asks_for_a_restart(env, monkeypatch, no_real_restarts):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    monkeypatch.setattr(updates, "installed_services", lambda home=None: ["notion", "course"])
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    fix = fix_of(assistant)
    assert fix.status == "restarting" and fix.issue_number == 1                   # 要望の issue を覚えたまま
    assert (cfg.repo_root / "src" / "app.py").read_text() == "y = 2\n"           # main に入った
    assert git(cfg.repo_root, "rev-parse", "main") == git(cfg.repo_root, "rev-parse", "origin/main")  # push した
    assert updates.pending_path(cfg).read_text().split() == [fix.base_commit, "0", "20.1"]  # 戻せるようにしてある
    assert not Path(fix.worktree).exists()                                         # worktree は片づけた
    await asyncio.wait_for(assistant.restart_requested.wait(), 1)                 # 作業がないので終了へ
    # 担当も一緒に入れ替える（テストでは本物の launchd には触らない）
    assert no_real_restarts == ["notion", "course"]


async def test_merge_stops_when_the_repository_has_uncommitted_changes(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    (cfg.repo_root / "src" / "app.py").write_text("人の書きかけ\n")

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    assert fix_of(assistant).status == "review"
    assert "コミットしていない変更がある" in "\n".join(slack.texts())
    assert (cfg.repo_root / "src" / "app.py").read_text() == "人の書きかけ\n"


async def test_merge_catches_up_when_main_moved_and_asks_again(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    (cfg.repo_root / "README.md").write_text("先に進んだ\n")
    git(cfg.repo_root, "add", "-A")
    git(cfg.repo_root, "commit", "-qm", "人が先に進めた")

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    fix = fix_of(assistant)
    assert fix.status == "review" and fix.base_commit == git(cfg.repo_root, "rev-parse", "HEAD")
    assert "乗せ直した" in "\n".join(slack.texts())


async def test_failed_checks_are_reported_and_nothing_is_merged(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    monkeypatch.setattr(improve_repo, "run_checks", lambda worktree: improve_repo.CommandResult(False, "1 failed"))
    before = git(cfg.repo_root, "rev-parse", "main")

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    assert git(cfg.repo_root, "rev-parse", "main") == before
    assert "確認が通らなかった" in "\n".join(slack.texts())


async def test_protected_change_is_not_offered_for_review(env):
    assistant, slack, claude, cfg = env

    def touches_guard(cwd: Path):
        (cwd / "config.example.toml").write_text("research_root = \"/tmp\"\n")

    await agreed(assistant, slack, claude, "柵を変えて")
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直した", "side_effect": touches_guard}]
    await second_yes(assistant)

    assert fix_of(assistant).status == "failed"
    assert "柵のファイルに触れています" in "\n".join(slack.texts())


# 起動したときの知らせ

async def started(assistant):
    """新しい版で起動し、Slack につながったあと（本体が入れ替えの結果を読み、モジュールの on_start を呼ぶ）。"""
    assistant.take_update()
    await assistant.modules_started()


async def test_announce_after_a_successful_update(env, fake_github):
    assistant, slack, claude, cfg = env
    fixes = assistant.modules["improve"].fixes
    fixes.start("C9", "20.1", "経過を細かく", merge_commit="abcdef1234")
    fixes.update("20.1", status="restarting")
    updates.mark_pending(cfg, "0123456789", "20.1")

    await started(assistant)

    assert fix_of(assistant).status == "done"
    assert not updates.pending_path(cfg).exists()
    assert "新しい版で起動したよ" in "\n".join(slack.texts())
    assert fake_github.calls == []                    # issue にしていない要望は何もしない


def merged_request(assistant, cfg, issue_number=7):
    """issue にした要望を取り込み、新しい版で起動する直前の状態にする。"""
    fixes = assistant.modules["improve"].fixes
    fixes.request("C9", "20.1", "経過を細かく", issue_number)
    fixes.start("C9", "20.1", "いいよ", merge_commit="abcdef1234")
    fixes.update("20.1", status="restarting")
    updates.mark_pending(cfg, "0123456789", "20.1")


async def test_announce_closes_the_issue_of_the_merged_request(env, fake_github):
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)

    await started(assistant)

    assert fake_github.closed() == [("7", "abcdef1 で取り込みました。")]
    assert fix_of(assistant).status == "done"


async def test_announce_goes_on_when_the_issue_cannot_be_closed(env, fake_github):
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)
    fake_github.fail["issue close"] = issues.IssueError("gh が失敗しました", "HTTP 502")

    await started(assistant)

    assert fix_of(assistant).status == "done"
    notice, = trouble_notices(slack)
    assert "issue #7 を閉じられませんでした" in notice


async def test_announce_survives_an_unexpected_error_while_closing(env, fake_github):
    """起動の途中で呼ばれるので、想定外の失敗でも止まらずに知らせる。"""
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)
    fake_github.fail["issue close"] = RuntimeError("壊れた")

    await started(assistant)

    notice, = trouble_notices(slack)
    assert "issue #7 を閉じられませんでした" in notice


async def test_announce_after_a_rollback(env, monkeypatch, fake_github):
    assistant, slack, claude, cfg = env
    fixes = assistant.modules["improve"].fixes
    fixes.request("C9", "20.1", "経過を細かく", 7)
    fixes.start("C9", "20.1", "経過を細かく")
    fixes.update("20.1", status="restarting")
    pushed = []
    monkeypatch.setattr(improve_repo, "push_revert", lambda root: pushed.append(root))
    updates.rolled_back_path(cfg).write_text("0123456789\n4\n20.1\n")

    await started(assistant)

    assert fix_of(assistant).status == "failed"
    assert not updates.rolled_back_path(cfg).exists()
    assert "起動できなかったので" in "\n".join(slack.texts())
    assert pushed == [cfg.repo_root]                  # 戻した取り消しを GitHub にも送る
    assert fake_github.closed() == []                 # 戻した要望の issue は開いたまま


async def test_an_update_asked_by_someone_else_is_left_alone(env, monkeypatch):
    """自分が頼んだ入れ替えでなければ（添えた1行が自分のスレッドでなければ）、何もしない。"""
    assistant, slack, claude, cfg = env
    monkeypatch.setattr(improve_repo, "push_revert", lambda root: pytest.fail("頼んでいない取り消しを送った"))
    updates.rolled_back_path(cfg).write_text("0123456789\n4\nほかのモジュール\n")
    await started(assistant)
    assert slack.texts() == []


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
    assert fix_of(assistant).status == "planning"   # 1回目では着手しない
    assert "この直し方で進めていい？" in "\n".join(slack.texts())

    slack.replies += [{"user": "UME", "ts": "20.3", "text": "いいよ"},
                      {"user": "UBOT", "bot_id": "B1", "ts": "20.4", "text": "これで進めていい？"}]
    claude.behaviors = [{"text": "じゃあやるね\n🛠 着手"}, {"text": "直した", "side_effect": edits_code()}]
    await second_yes(assistant)
    assert fix_of(assistant).status == "review"


def test_the_marks_are_hidden_but_nothing_else():
    text = "直したよ。\n:memo: 件名: x\n🛠 着手\n📦 取り込み"
    assert strip_lines(text, HIDDEN) == "直したよ。\n:memo: 件名: x"


# 途中で止まったとき

async def test_unexpected_error_while_fixing_marks_the_improvement_failed(env, monkeypatch):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直したよ", "side_effect": edits_code()}]

    def broken(worktree, message):
        raise RuntimeError("git が落ちた")
    monkeypatch.setattr(improve_repo, "commit_all", broken)
    await second_yes(assistant)

    fix = fix_of(assistant)
    assert fix.status == "failed" and "git が落ちた" in fix.detail
    assert "直している途中で止まった" in "\n".join(slack.texts())


async def test_fix_left_working_by_a_restart_is_marked_interrupted(env):
    assistant, slack, claude, cfg = env
    assistant.modules["improve"].fixes.start("C9", "20.1", "経過を細かく")

    await started(assistant)

    fix = fix_of(assistant)
    assert fix.status == "failed" and fix.detail == "中断"
    assert "中断した" in "\n".join(slack.texts())


async def test_push_failure_undoes_the_local_merge_and_keeps_review(env, monkeypatch):
    assistant, slack, claude, cfg = await prepared(env, monkeypatch)
    before = git(cfg.repo_root, "rev-parse", "main")
    git(cfg.repo_root, "remote", "set-url", "origin", str(cfg.repo_root.parent / "missing.git"))

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.2", "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)

    assert git(cfg.repo_root, "rev-parse", "main") == before                    # 手元の main は元のまま
    assert fix_of(assistant).status == "review"                                # もう一度「いいよ」でやり直せる
    assert not updates.pending_path(cfg).exists()
    assert not assistant.restart_requested.is_set()
    assert "push できなかった" in "\n".join(slack.texts())


# 片づけと、本体の表から写した記録

async def test_finished_worktrees_and_old_talks_are_cleaned_once_a_day(env):
    assistant, slack, claude, cfg = env
    module = assistant.modules["improve"]
    fixes = module.fixes
    busy = module.worktrees / "improve-30-1"
    done = module.worktrees / "improve-31-1"
    for path in (busy, done, module.talks / "30.1", module.talks / "31.1", module.talks / "32.1"):
        path.mkdir(parents=True)
    fixes.start("C9", "30.1", "直している", worktree=str(busy))
    fixes.start("C9", "31.1", "終わった", worktree=str(done))
    fixes.update("31.1", status="done")
    fixes.request("C9", "32.1", "相談中")

    await module.tick(datetime(2026, 9, 28, 0, 1))
    assert busy.exists() and not done.exists()
    assert sorted(p.name for p in module.talks.iterdir()) == ["30.1", "32.1"]
    # 同じ日には2回片づけない
    done.mkdir()
    await module.tick(datetime(2026, 9, 28, 12, 0))
    assert done.exists()


def test_old_improvements_are_copied_once_into_the_module_records(tmp_path):
    path = tmp_path / "kei-agent.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE improvements (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            thread_ts TEXT NOT NULL UNIQUE, request TEXT NOT NULL, status TEXT NOT NULL, branch TEXT, worktree TEXT,
            base_commit TEXT, merge_commit TEXT, detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            issue_number INTEGER);
        INSERT INTO improvements (channel, thread_ts, request, status, merge_commit, created_at, updated_at, issue_number)
            VALUES ('C9', '20.1', '経過を細かく', 'done', 'abcdef1234', 100.0, 200.0, 7);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings VALUES ('agent.self_fix.provider', 'codex');
    """)
    conn.commit()
    conn.close()

    store = Store(path)
    from kei_agent.records import Records
    from kei_agent_modules.improve.fixes import Fixes
    fix = Fixes(Records(store, "improve")).get("20.1")
    assert (fix.status, fix.issue_number, fix.merge_commit, fix.branch) == ("done", 7, "abcdef1234", "")
    # App Home で選んだ AI も、モジュールの実行役の名前に写す
    assert store.setting("agent.improve.provider") == "codex" and store.setting("agent.self_fix.provider") is None
    # 本体の表は念のため残す。写すのは一度だけ
    Fixes(Records(store, "improve")).update("20.1", status="failed")
    again = Fixes(Records(Store(path), "improve")).get("20.1")
    assert again.status == "failed"
    assert store.conn.execute("SELECT COUNT(*) FROM improvements").fetchone()[0] == 1


def test_the_old_agents_self_fix_table_still_reads(tmp_path):
    """config.toml に前の名前の [agents.self_fix] が残っていても読める（自己改善の実行役 improve として）。"""
    from kei_agent.config import load_config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[agents.self_fix]\nprovider = "codex"\n', encoding="utf-8")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.agent_profiles["improve"].provider == "codex" and "self_fix" not in config.agent_profiles


def test_the_improve_module_takes_the_kei_agent_channel():
    from kei_agent import modules

    spec = modules.builtin()["improve"]
    assert spec.core_channels == ("improve",) and spec.port is None
    assert (spec.actor.files, spec.actor.shell, spec.actor.default_use_case) == ("write", True, "improve_design")
    assert {u.name for u in spec.actor.use_cases} == {"improve_design", "improve_fix", "improve_issue"}
    assert (spec.path / spec.actor.prompt).name == "improve.md"
