"""自己改善のモジュール（段階3の自己改善の②。modules/improve/）。Slack から Kei Agent 自身を直す流れ。

Kei Agent のチャンネルで要望を聞き、公開の issue にし、案を相談して、worktree で直し、確認してから main に取り込んで、
新しい版で起動し直す。起動したときに結果を知らせる。記録はモジュールの記録（本体の表からは一度だけ写す）。
"""

import asyncio
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from fakes import FakeAI, git, make_assistant

from kei_agent.configuration.config import AgentProfile
from kei_agent.conversation.request import Request
from kei_agent.conversation.slack_text import strip_lines
from kei_agent.execution import guard, runner, updates
from kei_agent.testing.kit import settle
from kei_agent_modules.improve import issues
from kei_agent_modules.improve import repo as improve_repo
from kei_agent_modules.improve.module import HIDDEN


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
    claude = FakeAI()
    monkeypatch.setattr(runner, "run_model", claude)
    # 本体が外で回す全体のテスト（本物はこのテスト自身を回してしまう）
    monkeypatch.setattr(improve_repo, "run_checks", lambda worktree: improve_repo.CommandResult(True, "テストは通った"))
    assistant, slack = make_assistant(config, store, {"C9": "0-kei-agent", "C1": "vlm"})
    return assistant, slack, claude, config




def fix_of(assistant, thread_ts="20.1"):
    """自己改善のモジュールの、そのスレッドの記録。"""
    return assistant.modules["improve"].fixes.get(thread_ts)


def edits_code(text="y = 2\n"):
    def side_effect(cwd: Path):
        (cwd / "src" / "app.py").write_text(text)
    return side_effect


async def mention(assistant, text, ts="20.1"):
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": ts, "text": f"<@UBOT> {text}".rstrip()})
    await settle(assistant)


async def agreed(assistant, slack, claude, request="直して"):
    """依頼 → 案（「こう直すけど、いい？」）まで進めたスレッドにする。"""
    claude.behaviors = [{"text": "こう直すけど、いい？"}]
    await mention(assistant, request)
    slack.replies = [{"user": "UME", "ts": "20.1", "text": request},
                     {"user": "UBOT", "bot_id": "B1", "ts": "20.2", "text": "こう直すけど、いい？"}]


async def yes(assistant, ts="20.3"):
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": ts, "thread_ts": "20.1", "text": "いいよ"})
    await settle(assistant)


# コミットの件名

def test_commit_message_uses_the_subject_claude_wrote():
    summary = "**やったこと**\n\nログの進捗行を間引いた。\n\n📝 件名: ジョブのログから進捗の行を間引く"
    message = improve_repo.commit_message(summary, 3)
    assert message.splitlines()[0] == "ジョブのログから進捗の行を間引く"
    assert "件名:" not in message and "ログの進捗行を間引いた。" in message
    assert message.splitlines()[1] == "" and "**やったこと**" in message   # 空行を残す
    # リポジトリは公開なので、AI が件名を書かなくても要望の原文は使わない（issue の番号で書く）
    assert improve_repo.commit_message("直したよ", 13).splitlines()[0] == "#0-kei-agent の要望 #13 を直す"
    assert improve_repo.commit_message("直したよ").splitlines()[0] == "#0-kei-agent の要望を直す"


def test_fix_prompt_keeps_the_full_suite_out_of_the_sandbox():
    """AI の作業場では全体のテストが通らない（固まる）ので、関係するテストだけを前面で回させる。"""
    assert "全体のテストは" in improve_repo.FIX_PROMPT and "裏で流したまま終えない" in improve_repo.FIX_PROMPT
    assert "`uv run --frozen --group agents pytest -q` と" not in improve_repo.FIX_PROMPT


def test_stop_leftovers_stops_only_processes_inside_the_worktree(tmp_path):
    inside, outside = tmp_path / "wt", tmp_path / "other"
    inside.mkdir()
    outside.mkdir()
    procs = [subprocess.Popen(["sleep", "30"], cwd=folder) for folder in (inside, outside)]
    try:
        assert improve_repo.stop_leftovers(inside) == [procs[0].pid]
        assert procs[0].wait(5) != 0 and procs[1].poll() is None
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


def test_commit_all_does_not_pick_up_pytest_temp_folders(repo):
    """テストが worktree の中に作る pytest-of-*/ を、コミットに拾わないようにする。"""
    (repo / ".gitignore").write_text((Path(__file__).resolve().parents[1] / ".gitignore").read_text())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "gitignore を足す")
    (repo / "pytest-of-keitaro" / "pytest-1").mkdir(parents=True)
    (repo / "pytest-of-keitaro" / "pytest-1" / "config.toml").write_text("x = 1\n")
    (repo / "src" / "app.py").write_text("x = 2\n")

    improve_repo.commit_all(repo, "直す")

    committed = git(repo, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
    assert "src/app.py" in committed
    assert not any("pytest-of-keitaro" in f for f in committed)


# やりとりから着手まで

async def test_new_request_becomes_a_public_issue_and_plans_without_writing_code(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env
    seen = []

    async def summarize(run_ai, text, has_secret):
        seen.append(text)
        return issues.Summary("作業中の経過を細かく見せる", "- 作業中の様子を短く出す")

    monkeypatch.setattr(issues, "summarize", summarize)
    await mention(assistant, "経過をもっと細かく")

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
    from kei_agent.execution.agent_policy import policy_of
    from kei_agent.workspaces.themes import ChannelKind, Workspace
    settings_json = guard.build_settings(cfg, Workspace("research-agent", ChannelKind.IMPROVE, call["cwd"],
                                                        module="improve"), policy_of("improve"))
    allow = settings_json["permissions"]["allow"]
    assert f"Read(/{cfg.repo_root}/**)" in allow and f"Edit(/{call['cwd']}/**)" in allow
    assert f"Edit(/{cfg.repo_root}/**)" not in allow


async def test_a_retried_request_does_not_open_a_second_issue(env, fake_github):
    """上限や再起動で止まった依頼をやり直しても、issue は1つのまま。"""
    assistant, slack, claude, cfg = env
    await mention(assistant, "経過をもっと細かく")

    await assistant.submit(Request("C9", "kei-agent", "20.1", "20.1", "経過をもっと細かく"))
    await settle(assistant)

    assert len(fake_github.created()) == 1
    assert fix_of(assistant).issue_number == 1


def trouble_notices(slack) -> list[str]:
    """改善チャンネルに、スレッドの外で出した知らせ。"""
    return [kw["text"] for kw in slack.posted() if kw["channel"] == "C9" and "thread_ts" not in kw]


@pytest.mark.parametrize("error, said", [
    (issues.IssueError("gh が失敗しました", "HTTP 401: Bad credentials"),
     "GitHub の issue にできませんでした（gh が失敗しました）"),
    (RuntimeError("壊れた"), "GitHub の issue にできませんでした"),          # 想定外の失敗も知らせる
])
async def test_request_stays_in_slack_when_gh_fails(env, fake_github, error, said):
    assistant, slack, claude, cfg = env
    fake_github.fail["issue create"] = error

    await mention(assistant, "経過をもっと細かく")

    assert fix_of(assistant).issue_number is None
    notice, = trouble_notices(slack)
    assert said in notice
    assert "Bad credentials" not in "\n".join(slack.texts())      # 詳しい中身はログにだけ残す
    assert not (cfg.overview_dir / "backlog.md").exists()          # ファイルには逃がさない
    assert len(claude.calls) == 1                                  # 案は考える


async def test_an_unsafe_summary_opens_no_issue(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env

    async def summarize(run_ai, text, has_secret):
        raise issues.IssueError("要約が公開の条件に合いません", "URL を含む")

    monkeypatch.setattr(issues, "summarize", summarize)
    await mention(assistant, "経過をもっと細かく")

    assert fake_github.calls == []
    notice, = trouble_notices(slack)
    assert "要約が公開の条件に合いません" in notice


async def test_without_a_provider_it_says_why_no_issue_was_made(env, fake_github, monkeypatch):
    assistant, slack, claude, cfg = env
    config = replace(cfg, agent_profiles={**cfg.agent_profiles, "improve": AgentProfile()})
    object.__setattr__(assistant, "config", config)
    await mention(assistant, "経過をもっと細かく")

    assert fake_github.calls == []
    thread = [kw["text"] for kw in slack.posted() if kw.get("thread_ts") == "20.1"]
    assert any("選ばれていない" in text and "issue にしなかった" in text for text in thread)


async def test_a_request_without_words_opens_no_issue(env, fake_github):
    assistant, slack, claude, cfg = env
    await mention(assistant, "")

    assert fake_github.calls == []
    assert "issue にはしなかった" in "\n".join(slack.texts())


async def test_improve_channel_intro_says_requests_become_public_issues(env):
    assistant, slack, claude, cfg = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C9"})
    intro = slack.texts()[-1]
    assert "公開の GitHub issue" in intro and "backlog" not in intro


async def test_one_yes_fixes_merges_pushes_and_restarts(env, monkeypatch, no_real_restarts):
    """案への「いいよ」1回で、直す・テスト・取り込み・push・入れ替えまで進む（途中で確かめない）。"""
    assistant, slack, claude, cfg = env
    monkeypatch.setattr(updates, "installed_services", lambda home=None: ["notion", "course"])
    await agreed(assistant, slack, claude)
    claude.behaviors = [
        {"text": "じゃあやるね\n🛠 着手"},
        {"text": "直したよ。テストは通った\n📝 件名: app.py の値を直す", "side_effect": edits_code()},
    ]
    await yes(assistant)

    fix = fix_of(assistant)
    assert fix.status == "restarting" and fix.issue_number == 1 and fix.branch == "kei-agent/improve-20-1"
    assert claude.calls[-1]["cwd"].parent == cfg.module_state("improve").resolve() / "worktrees"
    assert git(cfg.repo_root, "log", "-1", "--format=%s") == "app.py の値を直す"
    assert (cfg.repo_root / "src" / "app.py").read_text() == "y = 2\n"           # main に入った
    assert git(cfg.repo_root, "rev-parse", "main") == git(cfg.repo_root, "rev-parse", "origin/main")  # push した
    assert updates.pending_path(cfg).read_text().split() == [fix.base_commit, "0", "20.1"]  # 戻せるようにしてある
    assert not Path(fix.worktree).exists()                                         # worktree は片づけた
    texts = "\n".join(slack.texts())
    assert "直し始めるね" in texts and "`src/app.py`" in texts and "取り込んでいいか" not in texts
    assert any(t.startswith("<@UME> 📦 直して、取り込んで GitHub に push したよ") for t in slack.texts())
    upload, = [kw for name, kw in slack.calls if name == "files_upload_v2"]
    assert upload["file_uploads"][0]["filename"] == "change.diff"
    await asyncio.wait_for(assistant.restart_requested.wait(), 1)                 # 作業がないので終了へ
    assert no_real_restarts == ["notion", "course"]                                # 担当も一緒に入れ替える


async def working(assistant, slack, claude):
    """案に合意して、裏で直している途中のスレッドにする（直しの AI はまだ答えていない）。"""
    await agreed(assistant, slack, claude)
    assistant.modules["improve"].fixes.start("C9", "20.1", "直して", branch="kei-agent/improve-20-1")
    claude.calls.clear()


async def test_messages_while_fixing_never_start_the_same_fix_again(env):
    """直している途中に様子を聞かれたら、相談の AI に聞かず（AI は裏の直しを知らない）、記録から答える。
    ほかの返信では、直しが裏で進んでいることを AI に伝え、同じ直しを自分で始めさせない。"""
    assistant, slack, claude, cfg = env
    await working(assistant, slack, claude)
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.5", "thread_ts": "20.1", "text": "直った？"})
    await settle(assistant)
    assert claude.calls == []
    assert "まだ直しているところだよ" in slack.texts()[-1]
    assert fix_of(assistant).status == "working"

    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.6", "thread_ts": "20.1", "text": "おけ"})
    await settle(assistant)
    call, = claude.calls
    assert "裏で進んでいる" in call["prompt"] and "あなたは直さない" in call["prompt"]
    assert call["prompt"].rstrip().endswith("おけ")


@pytest.mark.parametrize("passes_after_retry", [True, False])
async def test_failing_full_checks_are_handed_back_once_before_merging(env, monkeypatch, passes_after_retry):
    """外で回した全体のテストが落ちたら、結果を渡して1回だけ直させ、通ってから取り込む。まだ落ちれば取り込まない。"""
    assistant, slack, claude, cfg = env
    results = [improve_repo.CommandResult(False, "FAILED tests/test_schedule.py::test_old"),
               improve_repo.CommandResult(passes_after_retry, "2回目"), improve_repo.CommandResult(True, "通った")]
    monkeypatch.setattr(improve_repo, "run_checks", lambda worktree: results.pop(0))
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直した", "side_effect": edits_code()},
                        {"text": "古いテストも直した", "side_effect": edits_code("y = 3\n")}]
    await yes(assistant)

    assert "FAILED tests/test_schedule.py::test_old" in claude.prompts()[-1]
    if passes_after_retry:
        assert fix_of(assistant).status == "restarting"
    else:
        assert fix_of(assistant).status == "failed"
        assert any("全体のテストが通らなかった" in t for t in slack.texts())
        assert not any("push したよ" in t for t in slack.texts())


async def test_only_one_improvement_at_a_time(env):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude, "1つめ")
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直した", "side_effect": edits_code()}, {"text": "🛠 着手"}]
    await yes(assistant)
    claude.behaviors = [{"text": "案だよ"}, {"text": "🛠 着手"}]
    await mention(assistant, "2つめ", ts="21.1")
    slack.replies = [{"user": "UME", "ts": "21.1", "text": "2つめ"},
                     {"user": "UBOT", "bot_id": "B1", "ts": "21.2", "text": "こう直すけど、いい？"}]
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "21.4", "thread_ts": "21.1", "text": "いいよ"})
    await settle(assistant)
    assert fix_of(assistant, "21.1").status == "planning"
    assert "先に進んでいる直しがある" in "\n".join(slack.texts())


# 取り込みと再起動

async def fixed(env, *, while_fixing=None):
    """案に「いいよ」と言い、直させる（while_fixing は、直している間に起きること）。"""
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)

    def fix(cwd: Path):
        edits_code()(cwd)
        if while_fixing:
            while_fixing()
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直したよ", "side_effect": fix}]
    await yes(assistant)
    return assistant, slack, claude, cfg


async def test_merge_catches_up_when_main_moved_and_goes_on(env):
    """直している間に main が先に進んでいたら、その上に乗せ直して、確かめずにそのまま取り込む。"""
    cfg = env[3]

    def someone_commits():
        (cfg.repo_root / "README.md").write_text("先に進んだ\n")
        git(cfg.repo_root, "add", "-A")
        git(cfg.repo_root, "commit", "-qm", "人が先に進めた")
    assistant, slack, claude, cfg = await fixed(env, while_fixing=someone_commits)

    assert fix_of(assistant).status == "restarting"
    assert (cfg.repo_root / "README.md").exists() and (cfg.repo_root / "src" / "app.py").read_text() == "y = 2\n"


async def test_failed_checks_before_merging_are_reported_and_nothing_is_merged(env, monkeypatch):
    results = [improve_repo.CommandResult(True, "通った"), improve_repo.CommandResult(False, "1 failed")]
    monkeypatch.setattr(improve_repo, "run_checks", lambda worktree: results.pop(0))
    before = git(env[3].repo_root, "rev-parse", "main")
    assistant, slack, claude, cfg = await fixed(env)

    assert git(cfg.repo_root, "rev-parse", "main") == before
    notice, = [t for t in slack.texts() if "確認が通らなかった" in t]
    assert notice.startswith("<@UME> ⚠️")


async def test_protected_change_is_not_offered_for_review(env):
    assistant, slack, claude, cfg = env

    def touches_guard(cwd: Path):
        (cwd / "config.example.toml").write_text("research_root = \"/tmp\"\n")

    await agreed(assistant, slack, claude, "柵を変えて")
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直した", "side_effect": touches_guard}]
    await yes(assistant)

    assert fix_of(assistant).status == "failed"
    assert "柵のファイルに触れています" in "\n".join(slack.texts())


# 起動したときの知らせ

async def started(assistant):
    """新しい版で起動し、Slack につながったあと（本体が入れ替えの結果を読み、モジュールの on_start を呼ぶ）。"""
    assistant.take_update()
    await assistant.modules_started()


def merged_request(assistant, cfg, issue_number=7):
    """要望を取り込み、新しい版で起動する直前の状態にする（issue_number が None なら issue にしていない要望）。"""
    fixes = assistant.modules["improve"].fixes
    if issue_number is not None:
        fixes.request("C9", "20.1", "経過を細かく", issue_number)
    fixes.start("C9", "20.1", "いいよ", merge_commit="abcdef1234")
    fixes.update("20.1", status="restarting")
    updates.mark_pending(cfg, "0123456789", "20.1")


@pytest.mark.parametrize("issue_number", [7, None])
async def test_announce_after_a_successful_update_closes_its_issue(env, fake_github, issue_number):
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg, issue_number)

    await started(assistant)

    fix = fix_of(assistant)
    assert fix.status == "done"
    assert not updates.pending_path(cfg).exists()
    notice, = [t for t in slack.texts() if "新しい版で起動したよ" in t]
    assert notice.startswith("<@UME> ✅")
    if issue_number is None:
        assert fake_github.calls == []                # issue にしていない要望は何もしない
    else:
        assert fake_github.closed() == [("7", "abcdef1 で取り込みました。", "completed")] and fix.issue_closed


@pytest.mark.parametrize("error", [issues.IssueError("gh が失敗しました", "HTTP 502"), RuntimeError("壊れた")])
async def test_announce_goes_on_when_the_issue_cannot_be_closed(env, fake_github, error):
    """起動の途中で呼ばれるので、想定外の失敗でも止まらずに知らせる。"""
    assistant, slack, claude, cfg = env
    merged_request(assistant, cfg)
    fake_github.fail["issue close"] = error

    await started(assistant)

    assert fix_of(assistant).status == "done"
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
    notice, = [t for t in slack.texts() if "起動できなかったので" in t]
    assert notice.startswith("<@UME> ⚠️")
    assert pushed == [cfg.repo_root]                  # 戻した取り消しを GitHub にも送る
    assert fake_github.closed() == []                 # 戻した要望の issue は開いたまま


async def test_an_update_asked_by_someone_else_is_left_alone(env, monkeypatch):
    """自分が頼んだ入れ替えでなければ（添えた1行が自分のスレッドでなければ）、何もしない。"""
    assistant, slack, claude, cfg = env
    monkeypatch.setattr(improve_repo, "push_revert", lambda root: pytest.fail("頼んでいない取り消しを送った"))
    updates.rolled_back_path(cfg).write_text("0123456789\n4\nほかのモジュール\n")
    await started(assistant)
    assert slack.texts() == []


@pytest.mark.parametrize("answer, asks", [
    ("じゃあやるね\n🛠 着手", "こう直すけど、いい？"),
    ("もう直っていたよ\n✅ 解決済み", "直さずに、この要望を終わりにしていい？"),
])
async def test_markers_in_the_first_answer_ask_first(env, fake_github, answer, asks):
    """最初の依頼への返事に着手や終わりの合図があっても動かない。依頼者が一度答えてから。"""
    assistant, slack, claude, cfg = env
    claude.behaviors = [{"text": answer}]
    await mention(assistant, "直して")

    assert fix_of(assistant).status == "planning"
    assert fake_github.closed() == []
    assert asks in "\n".join(slack.texts())


async def test_a_status_question_opens_no_issue(env, fake_github):
    assistant, slack, claude, cfg = env
    await mention(assistant, "状況は？")
    assert fake_github.calls == []
    assert claude.calls[-1]["read_only"] is True


async def test_markers_in_the_answer_to_a_status_question_are_ignored(env, fake_github):
    """様子を聞かれただけの回（読むだけで動く）の返事に合図があっても、動かない。"""
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "まだ案のままだよ\n✅ 解決済み"}]
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": "20.5", "thread_ts": "20.1",
                                "text": "どうなってる？"})
    await settle(assistant)

    assert fix_of(assistant).status == "planning"
    assert fake_github.closed() == []
    assert claude.calls[-1]["read_only"] is True


def test_the_marks_are_hidden_but_nothing_else():
    text = "直したよ。\n:memo: 件名: x\n✅ テストは通った\n🛠 着手\n📦 取り込み\n✅ 解決済み\n🗑 見送り"
    assert strip_lines(text, HIDDEN) == "直したよ。\n:memo: 件名: x\n✅ テストは通った"


# 直さずに終わる

async def asked(assistant, slack, claude, request="直して"):
    """依頼 → 最初の返事まで進めたスレッドにする（要望は issue #1 になる）。"""
    claude.behaviors = [{"text": "もう直っていたよ。終わりにしていい？"}]
    await mention(assistant, request)
    slack.replies = [{"user": "UME", "ts": "20.1", "text": request},
                     {"user": "UBOT", "bot_id": "B1", "ts": "20.2", "text": "もう直っていたよ。終わりにしていい？"}]


async def reply(assistant, text="いいよ", ts="20.3"):
    await assistant.on_message({"channel": "C9", "user": "UME", "ts": ts, "thread_ts": "20.1", "text": text})
    await settle(assistant)


@pytest.mark.parametrize("said, answer, status, detail, comment, reason", [
    ("いいよ", "終わりにするね\n✅ 解決済み", "done", "直さずに解決", "直さずに解決しました。", "completed"),
    ("やっぱりやらない", "今回は見送るね\n🗑 見送り", "dropped", "見送り", "見送ることにしました。", "not planned"),
])
async def test_end_markers_end_the_request_and_close_its_issue(env, fake_github, said, answer, status, detail,
                                                               comment, reason):
    assistant, slack, claude, cfg = env
    await asked(assistant, slack, claude)
    claude.behaviors = [{"text": answer}]
    await reply(assistant, said)

    fix = fix_of(assistant)
    assert (fix.status, fix.detail, fix.issue_closed) == (status, detail, True)
    assert fake_github.closed() == [("1", comment, reason)]
    texts = "\n".join(slack.texts())
    assert f"issue #1 を閉じたよ（{detail}）" in texts and answer.splitlines()[-1] not in texts


@pytest.mark.parametrize("thread, marker", [(agreed, "🛠 着手"), (asked, "✅ 解決済み")])
async def test_markers_from_an_automatic_run_are_ignored(env, fake_github, thread, marker):
    """ジョブの完了などで自動で再開した回の返事に着手や終わりの行があっても、動かない（案のまま）。"""
    assistant, slack, claude, cfg = env
    await thread(assistant, slack, claude)
    claude.behaviors = [{"text": marker}]
    await assistant.submit(Request("C9", "0-kei-agent", "20.1", None, "ジョブが終わった", trigger="job"))
    await settle(assistant)

    assert fix_of(assistant).status == "planning"
    assert fake_github.closed() == []


async def test_closing_while_fixing_is_refused(env, fake_github):
    assistant, slack, claude, cfg = env
    await asked(assistant, slack, claude)
    assistant.modules["improve"].fixes.update("20.1", status="working")
    claude.behaviors = [{"text": "✅ 解決済み"}]
    await reply(assistant)

    assert fix_of(assistant).status == "working"
    assert fake_github.closed() == []
    assert "いま直している" in "\n".join(slack.texts())


async def test_merge_stops_on_uncommitted_changes_and_dropping_removes_the_worktree(env, fake_github):
    """人の書きかけがあれば取り込まずに止まり（書きかけは残す）、見送れば worktree と枝を片づける。"""
    cfg = env[3]
    assistant, slack, claude, cfg = await fixed(
        env, while_fixing=lambda: (cfg.repo_root / "config.example.toml").write_text("# 書きかけ\n"))
    worktree = Path(fix_of(assistant).worktree)
    assert fix_of(assistant).status == "review" and worktree.exists()
    assert "コミットしていない変更がある" in "\n".join(slack.texts())
    assert (cfg.repo_root / "config.example.toml").read_text() == "# 書きかけ\n"

    claude.behaviors = [{"text": "やめておくね\n🗑 見送り"}]
    await reply(assistant, "やっぱりやめる", ts="20.6")

    assert fix_of(assistant).status == "dropped"
    assert not worktree.exists()
    assert git(cfg.repo_root, "branch", "--list", "kei-agent/improve-20-1") == ""
    assert (cfg.repo_root / "src" / "app.py").read_text() == "x = 1\n"          # main は変えない
    assert fake_github.closed() == [("1", "見送ることにしました。", "not planned")]


async def test_a_request_without_an_issue_just_ends(env, fake_github):
    assistant, slack, claude, cfg = env
    fake_github.fail["issue create"] = issues.IssueError("gh が失敗しました")
    await asked(assistant, slack, claude)
    claude.behaviors = [{"text": "✅ 解決済み"}]
    await reply(assistant)

    assert fix_of(assistant).status == "done"
    assert fake_github.closed() == []
    assert "この要望は終わりにしたよ（直さずに解決）。" in slack.texts()


async def test_an_issue_that_cannot_be_closed_now_is_closed_later(env, fake_github):
    assistant, slack, claude, cfg = env
    await asked(assistant, slack, claude)
    fake_github.fail["issue close"] = issues.IssueError("gh が失敗しました", "HTTP 502")
    claude.behaviors = [{"text": "✅ 解決済み"}]
    await reply(assistant)

    fix = fix_of(assistant)
    assert fix.status == "done" and not fix.issue_closed
    assert "あとでやり直す" in "\n".join(slack.texts())
    notice, = trouble_notices(slack)
    assert "issue #1 を閉じられませんでした" in notice

    # 1日1回の見回りで閉じ直す。まだ閉じられなくても、知らせは増やさない
    module = assistant.modules["improve"]
    await module.tick(datetime(2026, 9, 29, 0, 1))
    assert not fix_of(assistant).issue_closed and len(trouble_notices(slack)) == 1
    del fake_github.fail["issue close"]
    await module.tick(datetime(2026, 9, 30, 0, 1))
    assert fix_of(assistant).issue_closed
    assert fake_github.closed()[-1] == ("1", "直さずに解決しました。", "completed")
    calls = len(fake_github.calls)
    await module.tick(datetime(2026, 10, 1, 0, 1))                               # 閉じたものは、もう触らない
    assert len(fake_github.calls) == calls


# 途中で止まったとき

async def test_unexpected_error_while_fixing_marks_the_improvement_failed(env, monkeypatch):
    assistant, slack, claude, cfg = env
    await agreed(assistant, slack, claude)
    claude.behaviors = [{"text": "🛠 着手"}, {"text": "直したよ", "side_effect": edits_code()}]

    def broken(worktree, message):
        raise RuntimeError("git が落ちた")
    monkeypatch.setattr(improve_repo, "commit_all", broken)
    await yes(assistant)

    fix = fix_of(assistant)
    assert fix.status == "failed" and "git が落ちた" in fix.detail
    assert "直している途中で止まった" in "\n".join(slack.texts())


async def test_fix_left_working_by_a_restart_is_marked_interrupted(env):
    assistant, slack, claude, cfg = env
    assistant.modules["improve"].fixes.start("C9", "20.1", "経過を細かく")

    await started(assistant)

    fix = fix_of(assistant)
    assert fix.status == "failed" and fix.detail == "中断"
    notice, = [t for t in slack.texts() if "中断した" in t]
    assert notice.startswith("<@UME> ⚠️")


async def test_push_failure_undoes_the_local_merge_and_can_be_retried(env):
    cfg = env[3]
    before = git(cfg.repo_root, "rev-parse", "main")
    origin = git(cfg.repo_root, "remote", "get-url", "origin")
    git(cfg.repo_root, "remote", "set-url", "origin", str(cfg.repo_root.parent / "missing.git"))
    assistant, slack, claude, cfg = await fixed(env)

    assert git(cfg.repo_root, "rev-parse", "main") == before                    # 手元の main は元のまま
    assert fix_of(assistant).status == "review"                                # 「やり直して」でやり直せる
    assert not updates.pending_path(cfg).exists()
    assert not assistant.restart_requested.is_set()
    assert "push できなかった" in "\n".join(slack.texts())

    git(cfg.repo_root, "remote", "set-url", "origin", origin)
    claude.behaviors = [{"text": "もう一度入れるね\n📦 取り込み"}]
    await reply(assistant, "やり直して", ts="20.6")
    assert fix_of(assistant).status == "restarting"
    assert git(cfg.repo_root, "rev-parse", "main") == git(cfg.repo_root, "rev-parse", "origin/main")


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


def test_the_improve_module_takes_the_kei_agent_channel():
    from kei_agent.framework import modules

    spec = modules.builtin()["improve"]
    assert spec.core_channels == ("improve",) and spec.port is None
    assert (spec.actor.data, spec.actor.default_use_case) == ("own", "improve_design")
    assert {u.name for u in spec.actor.use_cases} == {"improve_design", "improve_fix", "improve_issue"}
    assert (spec.path / spec.actor.prompt).name == "improve.md"
