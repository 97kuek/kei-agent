"""要望を、要約した公開の GitHub issue にする（docs/architecture.md の「自己改善」）。"""

import subprocess
from dataclasses import replace

import pytest

from kei_agent import issues, runner
from kei_agent.config import AgentProfile
from kei_agent.model_policy import UseCase

# conftest がすべてのテストで偽物に差し替える前の、本物
SUMMARIZE = issues.summarize
GH = issues.gh

REQUEST = "返事が来るまで経過が見えないので、作業中の様子を入力欄の下に1行で出してほしい。長い作業だと止まったのか分からない"
GOOD = issues.Summary("作業中の経過を短く見せる", "- 返事を待つ間も、いま何をしているかを1行で出す\n- 終わったら消す")


def model_returning(**fields):
    """runner.run_model の代わり。呼ばれた実行要求とプロンプトを残す。"""
    calls = []

    async def run_model(config, request, prompt, on_activity=None, on_text=None):
        calls.append((request, prompt))
        return runner.RunResult(provider=request.recipe.provider, **fields)

    return run_model, calls


# 要約を確かめる

def test_a_generalized_summary_passes():
    assert issues.problems(GOOD, REQUEST) == []


@pytest.mark.parametrize("title, body, reason", [
    ("", "- 本文", "題"),
    ("長" * 61, "- 本文", "題"),
    ("二行の\n題", "- 本文", "題"),
    ("題", "", "本文"),
    ("題", "\n".join(["- 行"] * 5), "本文"),
    ("題", "- " + "長" * 121, "本文"),
    ("Show progress while working", "- 本文", "日本語"),
    ("題", "- 詳しくは https://example.com/a を見る", "URL"),
    ("題", "- 手順は www.example.com にある", "URL"),
    ("題", "- example.slack.com のスレッドで受けた", "URL"),
    ("題", "- /Users/kei/src を直す", "パス"),
    ("題", "- ~/kei-agent/overview を直す", "パス"),
    ("題", "- <@U0123ABC> に知らせる", "メンション"),
    ("題", "- <#C0123ABC|kei-agent> で受ける", "メンション"),
    ("題", "- <!channel> に知らせる", "メンション"),
    ("#kei-agent の経過を見せる", "- 本文", "メンション"),
    ("題", "- ＃研究 のチャンネルで受ける", "メンション"),
    ("題", "- @someone に聞いてから直す", "メンション"),       # GitHub で人に届く
    ("題", "- kei@example の宛先に送る", "メンション"),
    # 本物のトークンに見える文字列をソースに置かない（GitHub の検出と guard.check_change に当たる）
    ("題", "- " + "xoxb" + "-1234567890-abcdefghij で送る", "秘密"),
])
def test_summaries_that_could_leak_or_break_the_format_are_rejected(title, body, reason):
    found = issues.problems(issues.Summary(title, body), REQUEST)
    assert any(reason in p for p in found), found


def test_copying_twenty_characters_of_the_request_is_rejected():
    close = replace(GOOD, body="- 返事が来るまで経過が見えないので、作業を短く見せる")      # 原文と19字同じ
    copied = replace(GOOD, body="- 返事が来るまで経過が見えないので、作業中を短く見せる")   # 20字同じ
    spaced = replace(GOOD, body="- 返事が来るまで 経過が見えないので、 作業中の様子を出す")  # 空白を挟んでも写し
    assert issues.problems(close, REQUEST) == []
    assert any("原文" in p for p in issues.problems(copied, REQUEST))
    assert any("原文" in p for p in issues.problems(spaced, REQUEST))


def test_parse_reads_the_json_even_with_words_around_it():
    assert issues.parse('はい。\n{"title": " 経過を見せる ", "body": "- 1行で出す"}\n以上') == \
        issues.Summary("経過を見せる", "- 1行で出す")
    assert issues.parse('{"title": "経過を見せる", "body": ["- 1行で出す", "- 終わったら消す"]}').body == \
        "- 1行で出す\n- 終わったら消す"
    assert issues.parse('{"title": "経過を見せる", "body": "- {中括弧} も読める"}').body == "- {中括弧} も読める"
    for text in ("JSON ではない", '{"title": "経過を見せる"}', '["title", "body"]', '{"title": 1, "body": "x"}'):
        assert issues.parse(text) is None


# 要約のモデル

async def test_summarize_uses_the_light_recipe_with_the_self_fix_provider(config, store, monkeypatch):
    run_model, calls = model_returning(text='{"title": "作業中の経過を短く見せる", "body": "- 様子を1行で出す"}')
    monkeypatch.setattr(runner, "run_model", run_model)
    # 振り分け（router）は Claude のまま。自己改善で選んだ Codex を使う
    config = replace(config, agent_profiles={**config.agent_profiles, "self_fix": AgentProfile(provider="codex")})

    summary = await SUMMARIZE(config, store, REQUEST)

    assert summary == issues.Summary("作業中の経過を短く見せる", "- 様子を1行で出す")
    (request, prompt), = calls
    recipe = request.recipe
    assert (recipe.actor, recipe.use_case, recipe.provider, recipe.model) == \
        ("router", UseCase.ROUTING, "codex", "gpt-6-luna")
    assert request.read_only and request.session_id is None
    assert request.workspace.cwd == config.state_dir / "classifier" / "self_fix"
    assert REQUEST in prompt and "JSON" in prompt


async def test_summarize_does_not_run_without_a_self_fix_provider(config, store, monkeypatch):
    run_model, calls = model_returning(text="{}")
    monkeypatch.setattr(runner, "run_model", run_model)
    config = replace(config, agent_profiles={**config.agent_profiles, "self_fix": AgentProfile()})

    with pytest.raises(issues.NoProvider):
        await SUMMARIZE(config, store, REQUEST)
    assert calls == []


@pytest.mark.parametrize("fields", [
    {"text": "要約できません"},                                              # JSON でない
    {"text": '{"title": "詳しくは https://example.com", "body": "- 見る"}'},  # 公開の条件に合わない
    {"is_error": True, "errors": ["boom"]},                                  # 失敗
    {"is_error": True, "limit_reset_at": 1.0},                               # 上限
])
async def test_summarize_refuses_unusable_output(config, store, monkeypatch, fields):
    run_model, _ = model_returning(**fields)
    monkeypatch.setattr(runner, "run_model", run_model)

    with pytest.raises(issues.IssueError) as raised:
        await SUMMARIZE(config, store, REQUEST)
    assert not isinstance(raised.value, issues.NoProvider)


async def test_summarize_survives_a_broken_runner(config, store, monkeypatch):
    async def broken(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(runner, "run_model", broken)
    with pytest.raises(issues.IssueError):
        await SUMMARIZE(config, store, REQUEST)


# gh

@pytest.mark.parametrize("url, slug", [
    ("https://github.com/97kuek/kei-agent.git", "97kuek/kei-agent"),
    ("https://github.com/97kuek/kei-agent", "97kuek/kei-agent"),
    ("git@github.com:97kuek/kei-agent.git", "97kuek/kei-agent"),
    ("ssh://git@github.com/97kuek/kei-agent.git", "97kuek/kei-agent"),
    ("https://x-access-token:abc@github.com/97kuek/kei-agent.git\n", "97kuek/kei-agent"),
    ("/tmp/origin.git", None),
    ("https://gitlab.com/97kuek/kei-agent.git", None),
])
def test_repository_comes_from_the_origin_url(url, slug):
    assert issues.parse_slug(url) == slug


def git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def test_repo_slug_reads_the_origin_of_the_repository(tmp_path):
    git(tmp_path, "init", "-q")
    assert await issues.repo_slug(tmp_path) is None     # origin がない
    git(tmp_path, "remote", "add", "origin", "git@github.com:97kuek/kei-agent.git")
    assert await issues.repo_slug(tmp_path) == "97kuek/kei-agent"


async def test_gh_works_on_the_origin_repository(config, monkeypatch):
    seen = []

    async def run_gh(*args):
        seen.append(args)
        return "done"

    async def repo_slug(root):
        assert root == config.repo_root
        return "97kuek/kei-agent"

    monkeypatch.setattr(issues, "run_gh", run_gh)
    monkeypatch.setattr(issues, "repo_slug", repo_slug)
    assert await GH(config, "issue", "close", "3") == "done"
    assert seen == [("issue", "close", "3", "--repo", "97kuek/kei-agent")]


async def test_gh_refuses_a_repository_that_is_not_on_github(config, monkeypatch):
    seen = []

    async def run_gh(*args):
        seen.append(args)
        return ""

    async def repo_slug(root):
        return None

    monkeypatch.setattr(issues, "run_gh", run_gh)
    monkeypatch.setattr(issues, "repo_slug", repo_slug)
    with pytest.raises(issues.IssueError):
        await GH(config, "issue", "list")
    assert seen == []


async def test_run_gh_returns_the_output_and_keeps_errors_for_the_log(tmp_path, monkeypatch):
    """本物の gh の代わりに、引数を返すだけのスクリプトを置く（GitHub には届かない）。"""
    fake = tmp_path / "gh"
    fake.write_text('#!/bin/sh\nif [ "$1" = "fail" ]; then echo "HTTP 401: Bad credentials" >&2; exit 1; fi\n'
                    'echo "$GH_PROMPT_DISABLED $*"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert (await issues.run_gh("issue", "view", "1")).strip() == "1 issue view 1"   # 確認を出さずに動かす
    with pytest.raises(issues.IssueError) as raised:
        await issues.run_gh("fail")
    assert "Bad credentials" in raised.value.detail and "Bad credentials" not in raised.value.reason


async def test_run_gh_without_gh_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(issues.IssueError, match="gh が見つかりません"):
        await issues.run_gh("issue", "list")


async def test_run_gh_that_cannot_be_started(tmp_path, monkeypatch):
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho x\n")
    fake.chmod(0o644)                                   # 実行できない
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(issues.IssueError):
        await issues.run_gh("issue", "list")


# issue を作る・閉じる

async def test_create_opens_a_labelled_issue_with_only_the_summary(config, fake_github):
    issue = await issues.create(config, GOOD)

    assert issue == issues.Issue(1, "https://github.com/97kuek/kei-agent/issues/1")
    assert fake_github.calls[0][:3] == ("label", "create", "kei-agent-request")
    assert fake_github.created() == [{"title": GOOD.title, "body": GOOD.body, "label": "kei-agent-request"}]


async def test_create_accepts_a_label_that_already_exists(config, fake_github):
    fake_github.fail["label create"] = issues.IssueError(
        "gh が失敗しました", 'label with name "kei-agent-request" already exists; use `--force` to update')
    assert (await issues.create(config, GOOD)).number == 1


async def test_create_stops_when_the_label_cannot_be_made(config, fake_github):
    fake_github.fail["label create"] = issues.IssueError("gh が失敗しました", "HTTP 403: Resource not accessible")
    with pytest.raises(issues.IssueError):
        await issues.create(config, GOOD)
    assert fake_github.created() == []


async def test_create_needs_the_number_of_the_new_issue(config, monkeypatch):
    async def gh(config, *args):
        return "作れたかどうか分からない出力\n"

    monkeypatch.setattr(issues, "gh", gh)
    with pytest.raises(issues.IssueError):
        await issues.create(config, GOOD)


async def test_close_leaves_the_short_sha_of_the_merge(config, fake_github):
    await issues.close(config, 12, "abcdef1234567890")
    assert fake_github.closed() == [("12", "abcdef1 で取り込みました。")]
