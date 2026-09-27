from __future__ import annotations

import pytest

from kei_agent.model_policy import ModelPolicyError, UseCase, explicit_use_case, is_manual, resolve


def test_research_execution_uses_provider_specific_recipes():
    # 研究の用途は、研究のモジュールの module.toml の [use_cases]
    codex = resolve("research", "codex", "research_execute")
    claude = resolve("research", "claude", "research_execute")

    assert (codex.model, codex.reasoning_effort) == ("gpt-6-sol", "high")
    assert (claude.model, claude.reasoning_effort) == ("claude-sonnet-5", "high")


def test_router_uses_lightweight_recipe_without_claude_thinking():
    claude = resolve("router", "claude", UseCase.ROUTING)

    assert (claude.model, claude.reasoning_effort) == ("claude-haiku-4-5", "")


def test_normal_use_cases_cannot_select_manual_top_model():
    with pytest.raises(ModelPolicyError, match="手動指定"):
        resolve("research", "codex", "manual_astra")


def test_owner_explicit_manual_label_can_select_the_exception(config, store):
    from kei_agent import settings
    from kei_agent.model_policy import resolve_selected

    settings.set_agent_provider(store, "research", "codex")
    use_case, prompt = explicit_use_case("research", "[[manual-astra]] 厳密な反証レビューをして")

    recipe = resolve_selected(config, store, "research", use_case, manual=is_manual(use_case))
    assert prompt == "厳密な反証レビューをして"
    assert (recipe.model, recipe.reasoning_effort, recipe.manual_only) == ("gpt-6-astra", "xhigh", True)


def test_unknown_actor_provider_or_use_case_is_rejected():
    with pytest.raises(ModelPolicyError, match="provider"):
        resolve("research", "", "research_execute")
    with pytest.raises(ModelPolicyError, match="actor"):
        resolve("unknown", "codex", "research_execute")


@pytest.mark.parametrize(("actor", "case"), [
    ("router", "research_execute"),
    ("work", "course_requirements"),
    ("course", "manual_astra"),
])
def test_resolve_rejects_use_case_outside_actor_policy(actor, case):
    with pytest.raises(ModelPolicyError):
        resolve(actor, "codex", case, manual=True)


def test_manual_fable_requires_research_and_claude():
    with pytest.raises(ModelPolicyError):
        resolve("research", "codex", "manual_fable", manual=True)


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


def test_config_rejects_old_model_override_and_defaults_to_unselected_provider(tmp_path):
    from kei_agent.config import ConfigError, load_config

    old = tmp_path / "old.toml"
    old.write_text('[agents.research]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\n')
    with pytest.raises(ConfigError, match="知らないキー"):
        load_config(old, env={})

    (tmp_path / "empty.toml").write_text("")
    config = load_config(tmp_path / "empty.toml", env={})
    assert config.agent_profiles["research"].provider == ""
    assert config.agent_profiles["router"].provider == ""


def test_selected_provider_resolves_its_fixed_recipe(config, store):
    from kei_agent import settings
    from kei_agent.model_policy import resolve_selected

    settings.set_agent_provider(store, "research", "codex")
    recipe = resolve_selected(config, store, "research", "research_execute")

    assert (recipe.provider, recipe.model, recipe.reasoning_effort) == ("codex", "gpt-6-sol", "high")


def test_lightweight_classifier_requires_valid_high_confidence_json():
    from kei_agent.model_classifier import parse

    research = frozenset({"research_extract", "research_design"})
    assert parse('{"use_case":"research_extract","confidence":0.9}', research) == "research_extract"
    assert parse('{"use_case":"research_design","confidence":0.7}', research) is None
    assert parse('{"use_case":"unknown","confidence":1}', research) is None


def test_lightweight_classifier_restricts_each_actor_to_its_own_cases():
    from kei_agent.model_classifier import parse

    # 大学と仕事のモジュールの用途（module.toml の [use_cases]）
    course = frozenset({"course_explain", "course_requirements", "course_compare", "course_degree_plan"})
    work = frozenset({"work_single_source", "work_cross_source", "work_decide"})
    assert parse('{"use_case":"course_requirements","confidence":0.9}', course) == "course_requirements"
    assert parse('{"use_case":"work_decide","confidence":0.9}', course) is None
    assert parse('{"use_case":"work_decide","confidence":0.9}', work) == "work_decide"


async def test_classifier_stops_on_a_provider_usage_limit(config, store, monkeypatch):
    from kei_agent import model_classifier, runner, settings

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
    from kei_agent import model_classifier, router, runner, settings

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
    from kei_agent.config import model_actors
    from kei_agent.model_policy import allowed_use_cases

    for actor in model_actors():
        for use_case in allowed_use_cases(actor):
            if is_manual(use_case):
                continue
            for provider in ("claude", "codex"):
                assert resolve(actor, provider, use_case).provider == provider
