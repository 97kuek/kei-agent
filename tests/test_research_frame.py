"""モジュールの枠の広がり（段階3の研究の①）。研究の担当を載せ替える前に、利用者のモジュールで確かめる。

用途の手動指定（[[名前]] と manual）、ほかのどれにも当たらないチャンネル（[channels] に "*"）、
チャンネルの作業場での会話（core.work）。
"""

import asyncio
from dataclasses import replace

import pytest
from fakes import FakeClaude, FakePueue, FakeSlack

from kei_agent import model_classifier, model_policy, modules, runner, themes
from kei_agent.assistant import Assistant
from kei_agent.config import ConfigError, load_config
from kei_agent.jobs import JobManager
from kei_agent.model_policy import ModelPolicyError

LAB_TOML = '''api = 1
name = "lab"
label = "実験"

[actor]
prompt = "lab.md"
files = "write"
shell = true
web = true
default_use_case = "lab_run"
classify = "ふつうの作業は lab_run、計画は lab_plan。"

[use_cases.lab_run]
claude = { model = "claude-sonnet-5", effort = "high" }

[use_cases.lab_plan]
claude = { model = "claude-opus-5", effort = "high" }

[use_cases.lab_deep]
manual = true
claude = { model = "claude-fable-5", effort = "high" }

[channels]
lab = ["*"]
'''

LAB_CODE = '''from kei_agent.api import Core, Request


class Module:
    def __init__(self, core: Core):
        self.core = core

    def welcome(self):
        return "実験のことを書いてね。"

    async def on_message(self, req: Request, skill: str = "", params=None) -> None:
        await self.core.work(req)
'''


def _lab(root, toml=LAB_TOML):
    folder = root / "lab"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text(toml, encoding="utf-8")
    (folder / "module.py").write_text(LAB_CODE, encoding="utf-8")
    (folder / "lab.md").write_text("# 実験の担当\n", encoding="utf-8")
    return folder


def _home(tmp_path, text=""):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")
    return home


# 用途の手動指定

def test_a_manual_use_case_is_chosen_only_by_name(tmp_path):
    """manual = true の用途は、依頼の頭に [[名前]] と書いたときだけ。分類器は選ばない。"""
    modules.register_user_modules(_lab(tmp_path).parent)
    spec = modules.known()["lab"]
    assert model_policy.explicit_use_case("lab", "[[lab-deep]] ちゃんと考えて") == ("lab_deep", "ちゃんと考えて")
    assert model_policy.explicit_use_case("lab", "[[lab-plan]]\n計画して") == ("lab_plan", "計画して")
    assert model_policy.explicit_use_case("lab", "[[nothing]] 何か") == (None, "[[nothing]] 何か")
    assert model_policy.explicit_use_case("lab", "ふつうの依頼") == (None, "ふつうの依頼")
    with pytest.raises(ModelPolicyError, match="手動指定"):
        model_policy.resolve("lab", "claude", "lab_deep")
    assert model_policy.resolve("lab", "claude", "lab_deep", manual=True).manual_only
    assert model_policy.is_manual("lab_deep") and not model_policy.is_manual("lab_run")
    # 研究の今の書き方（[[research-design]]、[[manual-fable]]）も同じ規則で読む
    assert model_policy.explicit_use_case("research", "[[research-design]] 設計") == ("research_design", "設計")
    assert model_policy.explicit_use_case("research", "[[manual-fable]] 深く")[0] == "manual_fable"
    assert spec.actor.default_use_case == "lab_run"


async def test_the_classifier_never_picks_a_manual_use_case(tmp_path, config, store, monkeypatch,
                                                            fake_model_classifier):
    modules.register_user_modules(_lab(tmp_path).parent)
    # 分類器の本物（conftest は偽物に差し替えて、本物を返す）
    monkeypatch.setattr(model_classifier, "classify_module", fake_model_classifier)
    seen = {}

    async def fake(_config, _store, _actor, _prompt, allowed, fallback, candidates, _guidance, *, provider=None):
        seen.update(allowed=allowed, candidates=candidates)
        return fallback

    monkeypatch.setattr(model_classifier, "_classify", fake)
    await model_classifier.classify_module(config, store, modules.known()["lab"], "計画して")
    assert seen["allowed"] == {"lab_run", "lab_plan"} and "lab_deep" not in seen["candidates"]


@pytest.mark.parametrize(("change", "message"), [
    (('[use_cases.lab_deep]\nmanual = true\n', '[use_cases.lab_deep]\n'), "manual = true"),
    (('default_use_case = "lab_run"', 'default_use_case = "lab_deep"'), "手動指定でない用途"),
])
def test_manual_only_models_and_defaults_are_checked(tmp_path, change, message):
    """いちばん強いモデル（fable・astra）は手動指定の用途でしか書けない。自由な質問の既定にも手動指定は使えない。"""
    home = _home(tmp_path, 'modules = ["lab"]\n')
    _lab(home / "modules", LAB_TOML.replace(*change))
    with pytest.raises((ConfigError, modules.ModuleError), match=message):
        load_config(env={"KEI_AGENT_HOME": str(home)})


# ほかのどれにも当たらないチャンネル

def test_one_module_can_take_every_unclaimed_channel(tmp_path):
    home = _home(tmp_path, 'modules = ["course", "lab"]\n')
    _lab(home / "modules")
    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    ws = themes.resolve(config, "10_vlm-counting")
    assert (ws.kind, ws.module, themes.actor_of(ws)) == (themes.ChannelKind.THEME, "lab", "lab")
    assert ws.cwd == config.research_root / "vlm-counting"
    course = themes.resolve(config, "20_course")
    assert (course.kind, course.module) == (themes.ChannelKind.MODULE, "course")
    # 受け持つモジュールが無ければ、研究テーマには担当がいない（そのチャンネルでは答えない）
    plain = themes.resolve(replace(config, modules=("course",)), "vlm")
    assert (plain.module, themes.actor_of(plain)) == ("", "")


def test_only_one_module_may_take_every_unclaimed_channel(tmp_path):
    """受け持てるのは、オンのモジュールのうち1つだけ（研究をオフにすれば、自分のモジュールに受け持たせられる）。"""
    home = _home(tmp_path, 'modules = ["lab", "lab2"]\n')
    _lab(home / "modules")
    other = home / "modules" / "lab2"
    other.mkdir()
    (other / "module.toml").write_text('api = 1\nname = "lab2"\n[channels]\nnotes = ["*"]\n', encoding="utf-8")
    (other / "module.py").write_text("class Module:\n    def __init__(self, core):\n        pass\n\n"
                                     "    async def on_message(self, req, skill='', params=None):\n        pass\n",
                                     encoding="utf-8")
    with pytest.raises(ConfigError, match="ほかのどれにも当たらないチャンネル"):
        load_config(env={"KEI_AGENT_HOME": str(home)})


def test_the_catch_all_mark_stands_alone(tmp_path):
    folder = _lab(tmp_path, LAB_TOML.replace('lab = ["*"]', 'lab = ["*", "vlm"]'))
    with pytest.raises(modules.ModuleError, match="それだけを書いて"):
        modules.load_spec(folder)


# チャンネルの作業場での会話（core.work）

class RecordingClaude(FakeClaude):
    """どの担当・用途で動かしたかも覚える。"""

    async def __call__(self, config, request, prompt, on_activity=None):
        result = await super().__call__(config, request, prompt, on_activity)
        self.calls[-1].update(actor=request.recipe.actor, use_case=str(request.recipe.use_case),
                              manual=request.recipe.manual_only)
        return result


@pytest.fixture
def lab(config, store, tmp_path, monkeypatch):
    modules.register_user_modules(_lab(tmp_path / "user-modules").parent)
    # 研究テーマは lab が受け持つ（組み込みの研究はオフ）
    config = replace(config, modules=(*[name for name in config.modules if name != "research"], "lab"),
                     module_channels={**config.module_channels, "lab": ("*",)},
                     agent_profiles={**config.agent_profiles, "lab": config.agent_profiles["work"]})
    slack = FakeSlack({"C1": "vlm"})
    claude = RecordingClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    return assistant, slack, claude


async def settle(assistant):
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks), return_exceptions=True)
        await asyncio.sleep(0)


async def test_a_module_answers_in_the_channel_workspace_like_research(lab, config):
    """テーマを受け持つモジュールの担当が、テーマのフォルダで会話して答える（続きは同じスレッドで）。"""
    assistant, slack, claude = lab
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> [[lab-deep]] 深く調べて"})
    await settle(assistant)
    call, = claude.calls
    assert (call["actor"], call["use_case"], call["manual"]) == ("lab", "lab_deep", True)
    assert call["cwd"] == config.research_root / "vlm" and "深く調べて" in call["prompt"]
    assert "[[lab-deep]]" not in call["prompt"]
    assert slack.streamed() == ["結果です"]
    assert ("reactions_add", {"channel": "C1", "timestamp": "10.1", "name": "white_check_mark"}) in slack.calls

    await assistant.on_message({"channel": "C1", "user": "UME", "ts": "10.2", "thread_ts": "10.1", "text": "続けて"})
    await settle(assistant)
    assert claude.calls[-1]["session_id"] == "sess-1" and claude.calls[-1]["use_case"] == "lab_run"


async def test_joining_a_theme_channel_shows_the_modules_welcome(lab, config):
    assistant, slack, claude = lab
    # フォルダがあるテーマ（無ければ、先に置き場所を聞く。test_theme_places.py）
    (config.research_root / "vlm").mkdir(parents=True)
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
    text, = slack.texts()
    assert str(config.research_root / "vlm") in text and text.endswith("実験のことを書いてね。")
    assert (config.research_root / "vlm").is_dir()


# 研究のモジュール（modules/research/）

def test_research_is_the_builtin_module_that_takes_theme_channels():
    spec = modules.builtin()["research"]
    assert spec.catch_all and spec.port == 8788 and spec.actor.shell and spec.actor.files == "write"
    manual = {u.name for u in spec.actor.use_cases if u.manual}
    assert manual == {"manual_astra", "manual_fable"} and spec.actor.default_use_case == "research_execute"
    assert (spec.path / spec.actor.prompt).name == "research.md"
    code = modules.load_agent(spec)
    assert [skill.id for skill in code.SKILLS] == ["ask", "submit-job", "list-jobs", "cancel-job", "forget-job"]


async def test_without_a_module_for_themes_the_channel_is_told_so(config, store, monkeypatch):
    """研究をオフにすると、研究テーマのチャンネルでは答えない（受け持つモジュールが無いと伝える）。"""
    slack = FakeSlack({"C1": "vlm"})
    claude = RecordingClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    config = replace(config, modules=tuple(name for name in config.modules if name != "research"))
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT")
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    await settle(assistant)
    assert claude.calls == [] and "受け持つモジュールがない" in slack.texts()[-1]
