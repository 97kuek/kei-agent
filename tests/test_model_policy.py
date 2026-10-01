from __future__ import annotations

import pytest

from kei_agent.model_policy import ModelPolicyError, UseCase, explicit_use_case, is_manual, resolve


@pytest.mark.parametrize("actor, provider, use_case, model, effort", [
    # 研究の用途は、研究のモジュールの module.toml の [use_cases]
    ("research", "codex", "research_execute", "gpt-6-sol", "high"),
    ("research", "claude", "research_execute", "claude-sonnet-5", "high"),
    ("router", "claude", UseCase.ROUTING, "claude-haiku-4-5", ""),     # 振り分けは軽く、考えさせない
])
def test_each_use_case_resolves_its_fixed_recipe(actor, provider, use_case, model, effort):
    recipe = resolve(actor, provider, use_case)
    assert (recipe.model, recipe.reasoning_effort) == (model, effort)


@pytest.mark.parametrize("actor, provider, use_case, manual, match", [
    ("research", "codex", "manual_astra", False, "手動指定"),     # ふつうの用途からは最上位を選べない
    ("research", "", "research_execute", False, "provider"),
    ("unknown", "codex", "research_execute", False, "actor"),
    ("router", "codex", "research_execute", True, None),          # 担当の外の用途
    ("work", "codex", "course_requirements", True, None),
    ("course", "codex", "manual_astra", True, None),
    ("research", "codex", "manual_fable", True, None),            # fable は研究の Claude だけ
])
def test_resolve_rejects_what_the_policy_does_not_allow(actor, provider, use_case, manual, match):
    with pytest.raises(ModelPolicyError, match=match):
        resolve(actor, provider, use_case, manual=manual)


def test_selected_provider_and_the_manual_label_resolve_their_recipes(config, store):
    """選んだ provider の固定のレシピになる。持ち主が [[manual-astra]] と書いたときだけ例外の最上位を使う。"""
    from kei_agent.configuration import settings
    from kei_agent.model_policy import resolve_selected

    settings.set_agent_provider(store, "research", "codex")
    recipe = resolve_selected(config, store, "research", "research_execute")
    assert (recipe.provider, recipe.model, recipe.reasoning_effort) == ("codex", "gpt-6-sol", "high")

    use_case, prompt = explicit_use_case("research", "[[manual-astra]] 厳密な反証レビューをして")
    recipe = resolve_selected(config, store, "research", use_case, manual=is_manual(use_case))
    assert prompt == "厳密な反証レビューをして"
    assert (recipe.model, recipe.reasoning_effort, recipe.manual_only) == ("gpt-6-astra", "xhigh", True)


@pytest.mark.parametrize("provider, model", [
    ("codex", "gpt-6-luna"),
    ("codex", "gpt-6-sol"),
    ("codex", "gpt-6-astra"),
    ("claude", "claude-haiku-4-5"),
    ("claude", "claude-sonnet-5"),
    ("claude", "claude-opus-5"),
    ("claude", "claude-fable-5"),
])
def test_only_approved_models_are_allowlisted(provider, model):
    from kei_agent.model_policy import is_allowed_model

    assert is_allowed_model(provider, model)
    assert not is_allowed_model(provider, "gpt-5.6-terra")


def test_config_without_a_table_leaves_every_provider_unselected(tmp_path):
    from kei_agent.configuration.config import load_config

    (tmp_path / "empty.toml").write_text("")
    config = load_config(tmp_path / "empty.toml", env={})
    assert config.agent_profiles["research"].provider == ""
    assert config.agent_profiles["router"].provider == ""


# 大学と仕事のモジュールの用途（module.toml の [use_cases]）
COURSE = frozenset({"course_explain", "course_requirements", "course_compare", "course_degree_plan"})
WORK = frozenset({"work_single_source", "work_cross_source", "work_decide"})
RESEARCH = frozenset({"research_extract", "research_design"})


@pytest.mark.parametrize("answer, allowed, expected", [
    ('{"use_case":"research_extract","confidence":0.9}', RESEARCH, "research_extract"),
    ('{"use_case":"research_design","confidence":0.7}', RESEARCH, None),      # 自信が低い
    ('{"use_case":"unknown","confidence":1}', RESEARCH, None),
    ('{"use_case":"course_requirements","confidence":0.9}', COURSE, "course_requirements"),
    ('{"use_case":"work_decide","confidence":0.9}', COURSE, None),            # ほかの担当の用途
    ('{"use_case":"work_decide","confidence":0.9}', WORK, "work_decide"),
])
def test_lightweight_classifier_takes_only_confident_answers_for_the_actor(answer, allowed, expected):
    from kei_agent.model_classifier import parse

    assert parse(answer, allowed) == expected


async def test_classifier_stops_on_a_provider_usage_limit(config, store, monkeypatch):
    from kei_agent import model_classifier, runner
    from kei_agent.configuration import settings

    settings.set_agent_provider(store, "research", "claude")

    async def limited(*_args, **_kwargs):
        return runner.RunResult(is_error=True, limit_reset_at=123.0, errors=["usage limit reached"])

    monkeypatch.setattr(runner, "run_model", limited)
    with pytest.raises(model_classifier.UsageLimited) as raised:
        await model_classifier._classify(
            config, store, "research", "実験ログを見て", frozenset({"research_extract", "research_execute"}),
            "research_execute", "research_extract", "抽出は research_extract。")
    assert raised.value.reset_at == 123.0


async def test_each_classifier_runs_in_its_own_directory(config, store, monkeypatch):
    """research と course の分類が同時に走っても、skill の置き場を取り合わない。"""
    from kei_agent import model_classifier, router, runner
    from kei_agent.configuration import settings

    seen = []

    async def record(_config, request, _prompt, *_args, **_kwargs):
        seen.append(request.workspace.cwd)
        return runner.RunResult(text='{"use_case":"research_extract","confidence":0.9}')

    monkeypatch.setattr(runner, "run_model", record)
    for actor in ("research", "course"):
        settings.set_agent_provider(store, actor, "codex")
    # conftest が差し替えた classify_module ではなく、本物の _classify を通す
    await model_classifier._classify(config, store, "research", "ログを見て", frozenset({"research_extract"}),
                                     "research_execute", "", "")
    await model_classifier._classify(config, store, "course", "課題の要件", frozenset({"course_explain"}),
                                     "course_explain", "", "")

    assert len(set(seen + [router.workspace(config).cwd])) == 3


def test_every_actor_use_case_has_a_recipe_on_both_providers():
    """Claude でも Codex でも同じ担当が動く（知識の担当を足したときに、片方だけ忘れないように）。"""
    from kei_agent.configuration.config import model_actors
    from kei_agent.model_policy import allowed_use_cases

    for actor in model_actors():
        for use_case in allowed_use_cases(actor):
            if is_manual(use_case):
                continue
            for provider in ("claude", "codex"):
                assert resolve(actor, provider, use_case).provider == provider
