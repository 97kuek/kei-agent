"""モジュールの枠の広がり（段階3の自己改善の①）。自己改善を載せ替える前に、利用者のモジュールで確かめる。

本体のチャンネル（Kei Agent のチャンネル）の会話を受け持つ（core_channels）、自分のフォルダで AI を動かす
（core.work の folder、core.run_ai）、柵の確認、新しい版での起動し直しとその結果（restart_for_update・
last_update・on_start）、入れ替えを待たせる（busy）、スレッドの履歴・添付・経過の表示。
"""

import asyncio
import subprocess
from dataclasses import replace

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack, write_config

from kei_agent import api, guard, modules, runner, themes, updates
from kei_agent.assistant import Assistant
from kei_agent.config import ConfigError, load_config
from kei_agent.jobs import JobManager
from kei_agent.request import Request
from kei_agent.themes import ChannelKind

FIXER_TOML = '''api = 1
name = "fixer"
label = "直し係"
core_channels = ["improve"]

[actor]
prompt = "fixer.md"
files = "write"
shell = true
web = false
default_use_case = "fixer_talk"

[use_cases.fixer_talk]
claude = { model = "claude-sonnet-5", effort = "high" }

[use_cases.fixer_edit]
claude = { model = "claude-sonnet-5", effort = "high" }

[use_cases.fixer_summary]
offline = true
claude = { model = "claude-haiku-4-5" }
'''

FIXER_CODE = '''from kei_agent.api import Core, Request

MARK = "🛠 着手"


class Module:
    def __init__(self, core: Core):
        self.core = core
        self.answers = []
        self.started = []

    def welcome(self):
        return "直したいことを書いてね。"

    async def on_message(self, req: Request, skill: str = "", params=None) -> None:
        folder = self.core.state_dir / "talk" / req.thread_ts
        self.answers.append(await self.core.work(req, folder=folder, hide=(MARK,)))

    async def on_start(self):
        self.started.append(self.core.last_update())
'''


def _fixer(root, toml=FIXER_TOML, code=FIXER_CODE):
    folder = root / "fixer"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text(toml, encoding="utf-8")
    if code is not None:
        (folder / "module.py").write_text(code, encoding="utf-8")
    (folder / "fixer.md").write_text("# 直し係\n", encoding="utf-8")
    return folder


def _home(tmp_path, text=""):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_config(home / "config.toml", text)
    return home


class RecordingClaude(FakeClaude):
    """どの担当・用途・作業場で、書き込みを許して動かしたかも覚える。"""

    async def __call__(self, config, request, prompt, on_activity=None):
        result = await super().__call__(config, request, prompt, on_activity)
        self.calls[-1].update(actor=request.recipe.actor, use_case=str(request.recipe.use_case),
                              kind=request.workspace.kind, read_only=request.read_only)
        return result


@pytest.fixture
def env(config, store, tmp_path, monkeypatch):
    modules.register_user_modules(_fixer(tmp_path / "user-modules").parent)
    # 組み込みの自己改善は外す（Kei Agent のチャンネルを受け持てるのは1つだけ）
    config = replace(config, modules=(*[name for name in config.modules if name != "improve"], "fixer"),
                     agent_profiles={**config.agent_profiles, "fixer": config.agent_profiles["work"]})
    slack = FakeSlack({"C9": "0-kei-agent", "C1": "vlm"})
    claude = RecordingClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    return assistant, slack, claude


async def settle(assistant):
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks), return_exceptions=True)
        await asyncio.sleep(0)


def req(text="直して", ts="20.1"):
    return Request("C9", "kei-agent", ts, ts, text)


# 本体のチャンネルの会話を受け持つ

def test_a_module_takes_the_kei_agent_channel(tmp_path):
    home = _home(tmp_path, 'modules = ["fixer"]\n')
    _fixer(home / "modules")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    ws = themes.resolve(config, "0-kei-agent")
    assert (ws.kind, ws.module, themes.actor_of(ws)) == (ChannelKind.IMPROVE, "fixer", "fixer")
    # 受け持つモジュールが無ければ、担当はいない（困りごとの知らせだけの場所になる）
    plain = themes.resolve(replace(config, modules=()), "kei-agent")
    assert (plain.module, themes.actor_of(plain)) == ("", "")


def test_only_known_core_channels_can_be_taken(tmp_path):
    folder = _fixer(tmp_path, FIXER_TOML.replace('core_channels = ["improve"]', 'core_channels = ["overview"]'))
    with pytest.raises(modules.ModuleError, match="受け持てない本体のチャンネル"):
        modules.load_spec(folder)
    folder = _fixer(tmp_path / "b", code="class Module:\n    def __init__(self, core):\n        pass\n")
    with pytest.raises(modules.ModuleError, match="on_message"):
        modules.load_code(modules.load_spec(folder))


def test_only_one_module_may_take_a_core_channel(tmp_path):
    home = _home(tmp_path, 'modules = ["fixer", "fixer2"]\n')
    _fixer(home / "modules")
    other = _fixer(home / "modules" / "x", FIXER_TOML.replace('name = "fixer"', 'name = "fixer2"')
                   .replace("fixer_", "fixer2_"))
    other.rename(home / "modules" / "fixer2")
    with pytest.raises(ConfigError, match="どちらも本体のチャンネル（improve）"):
        load_config(env={"KEI_AGENT_HOME": str(home)})


async def test_the_module_answers_in_its_own_folder_and_hides_its_marks(env, config):
    assistant, slack, claude = env
    claude.behaviors = [{"text": "こう直すね\n🛠 着手"}]
    await assistant.on_mention({"channel": "C9", "user": "UME", "ts": "20.1", "text": "<@UBOT> ログを細かく"})
    await settle(assistant)

    module = assistant.modules["fixer"]
    call, = claude.calls
    assert (call["actor"], call["use_case"], call["kind"]) == ("fixer", "fixer_talk", ChannelKind.IMPROVE)
    assert call["cwd"] == config.module_state("fixer") / "talk" / "20.1"
    assert module.answers == ["こう直すね\n🛠 着手"]                   # 合図の行はモジュールが受け取る
    assert slack.streamed() == ["こう直すね"]                          # Slack には出さない


async def test_joining_the_kei_agent_channel_shows_the_modules_welcome(env):
    assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C9"})
    text, = slack.texts()
    assert "確認が必要なこと" in text and text.endswith("直したいことを書いてね。")


async def test_the_folder_must_be_inside_the_modules_own_folder(env, tmp_path):
    assistant, slack, claude = env
    core = assistant.cores["fixer"]
    with pytest.raises(ValueError, match="core.state_dir"):
        await core.work(req(), folder=tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="core.state_dir"):
        await core.run_ai("fixer_edit", "直して", folder=core.state_dir / ".." / "research")
    # 研究テーマのチャンネルでは、テーマのフォルダを使う（folder は渡せない）
    with pytest.raises(ValueError, match="会話を受け持つチャンネルではありません"):
        await core.work(Request("C1", "vlm", "1.1", "1.1", "x"), folder=core.state_dir / "x")


# 1回だけ動かす AI

async def test_run_ai_reads_only_unless_given_a_folder(env, config):
    assistant, slack, claude = env
    core = assistant.cores["fixer"]
    claude.behaviors = [{"text": '{"title": "要約"}', "raw": True}]
    assert await core.run_ai("fixer_summary", "まとめて") == '{"title": "要約"}'
    call = claude.calls[-1]
    assert (call["use_case"], call["read_only"], call["cwd"]) == ("fixer_summary", True, config.module_workspace("fixer"))

    folder = core.state_dir / "worktrees" / "one"
    claude.behaviors = [{"text": "直したよ", "side_effect": lambda cwd: (cwd / "a.py").write_text("x = 1\n")}]
    text = await core.run_ai("fixer_edit", "直して", folder=folder, req=req(), status="改善中…")
    call = claude.calls[-1]
    assert api.final_answer(text) == "直したよ" and (folder / "a.py").exists()
    assert (call["kind"], call["read_only"], call["cwd"]) == (ChannelKind.FOLDER, False, folder.resolve())
    assert "改善中…" in slack.thinking() and slack.streamed()[-1] == "直したよ"


async def test_run_ai_raises_when_the_ai_fails(env):
    assistant, slack, claude = env
    claude.behaviors = [{"is_error": True, "errors": ["こわれた"], "text": ""}]
    with pytest.raises(api.AIError, match="こわれた"):
        await assistant.cores["fixer"].run_ai("fixer_edit", "直して", folder=assistant.cores["fixer"].state_dir / "w",
                                              req=req())
    assert assistant.idle.is_set()                                    # 入れ替えを待たせたままにしない


# 柵の確認

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


async def test_check_change_uses_the_core_fence(env, tmp_path):
    assistant, slack, claude = env
    repo = tmp_path / "repo"
    (repo / "src" / "kei_agent").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "kei_agent" / "guard.py").write_text("# ゆるめる\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    problems = await assistant.cores["fixer"].check_change(repo, base)
    assert any("柵のファイル" in p for p in problems)


def test_secrets_are_spotted_before_writing_in_public():
    assert api.contains_secret("鍵は xoxb-1234567890-abcdefghij です")
    assert not api.contains_secret("ログを細かくする")


# 新しい版での起動し直しと、その結果

async def test_restart_for_update_leaves_a_mark_and_waits_for_the_work(env, config, monkeypatch, no_real_restarts):
    assistant, slack, claude = env
    core = assistant.cores["fixer"]
    monkeypatch.setattr(updates, "installed_services", lambda home=None: ["notion", "course"])
    with core.busy():
        core.restart_for_update("0123456789", "20.1 の取り込み")
        await asyncio.sleep(0)
        assert not assistant.restart_requested.is_set()               # 作業中は待つ
    await asyncio.wait_for(assistant.restart_requested.wait(), 1)
    assert updates.pending_path(config).read_text().splitlines() == ["0123456789", "0", "20.1 の取り込み"]
    assert no_real_restarts == ["notion", "course"]                   # ほかのプロセスも一緒に


@pytest.mark.parametrize(("marker", "state"), [(updates.PENDING_NAME, "done"),
                                               (updates.ROLLED_BACK_NAME, "rolled_back")])
async def test_the_next_start_tells_the_module_what_happened(env, config, marker, state):
    assistant, slack, claude = env
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / marker).write_text("0123456789\n1\n20.1\n")
    assistant.take_update()
    await assistant.modules_started()
    assert assistant.modules["fixer"].started == [updates.Update(state, "0123456789", "20.1")]
    # 印は消える（残ると deploy/run.sh が前の版に戻す）
    assert not updates.pending_path(config).exists() and not updates.rolled_back_path(config).exists()


async def test_a_failing_on_start_is_reported_and_does_not_stop_the_others(env, monkeypatch):
    assistant, slack, claude = env

    async def broken():
        raise RuntimeError("こわれた")

    monkeypatch.setattr(assistant.modules["fixer"], "on_start", broken)
    await assistant.modules_started()
    assert "起動のときの処理が落ちました" in slack.texts()[-1]


# スレッドの履歴・添付・経過・Markdown の投稿

async def test_thread_helpers(env):
    assistant, slack, claude = env
    core = assistant.cores["fixer"]
    slack.replies = [{"ts": "20.1", "user": "UME", "text": "ログを細かく"},
                     {"ts": "20.2", "user": "UBOT", "text": "こう直すね"}]
    assert [m["ts"] for m in await core.thread_messages("C9", "20.1")] == ["20.1", "20.2"]
    history = await core.thread_history("C9", "20.1")
    assert "ログを細かく" in history and "こう直すね" in history

    await core.upload("C9", "20.1", "change.diff", "+x = 1\n")
    (_, sent), = [(n, kw) for n, kw in slack.calls if n == "files_upload_v2"]
    assert sent["thread_ts"] == "20.1" and sent["file_uploads"][0]["content"] == "+x = 1\n"

    await core.post("C9", "*変えたファイル*", thread_ts="20.1", markdown=True)
    assert slack.posted()[-1]["markdown_text"] == "*変えたファイル*" and "text" not in slack.posted()[-1]

    async with core.progress(req(), "取り込み中…"):
        assert "取り込み中…" in slack.thinking()


def test_the_module_folder_is_under_the_state_folder(env, config):
    assistant, slack, claude = env
    core = assistant.cores["fixer"]
    assert core.state_dir == config.state_dir / "modules" / "fixer" and core.state_dir.is_dir()
    assert core.repo_root == config.repo_root


# 本体の柵（guard.check_change）

@pytest.fixture
def repo(tmp_path):
    """Kei Agent のリポジトリに見立てた git リポジトリ。"""
    path = tmp_path / "fenced"
    (path / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(path))
    _git(path, "config", "user.email", "kei-agent@example.com")
    _git(path, "config", "user.name", "Kei Agent")
    (path / "src" / "app.py").write_text("x = 1\n")
    (path / "config.example.toml").write_text('research_root = "~/research"\n')
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "はじめ")
    _git(path, "checkout", "-q", "-b", "work")
    return path


def test_the_fence_rejects_protected_paths(repo):
    (repo / "config.example.toml").write_text('research_root = "/tmp"\n')
    _git(repo, "commit", "-qam", "柵を触る")
    assert any("柵のファイル" in p for p in guard.check_change(repo, "main", "HEAD"))


def test_the_fence_rejects_secrets(repo):
    (repo / "src" / "app.py").write_text('TOKEN = "' + "xoxb" + '-1234567890-abcdefghij"\n')
    _git(repo, "commit", "-qam", "鍵を書く")
    problems = guard.check_change(repo, "main", "HEAD")
    assert any("秘密情報" in p for p in problems), problems


def test_the_fence_accepts_a_normal_fix(repo):
    (repo / "src" / "app.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "直す")
    assert guard.check_change(repo, "main", "HEAD") == []
