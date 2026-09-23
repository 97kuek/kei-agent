from dataclasses import replace

import pytest

from kei_agent import settings
from kei_agent.config import MaintenanceConfig, ScheduleConfig


def test_domains_are_kept_per_theme(store):
    settings.allow_domain(store, "amr-query", "zenodo.org", "特徴量を落とすため")
    settings.allow_domain(store, "amr-query", "zenodo.org", "二度目")  # 同じものは1つにまとめる
    settings.allow_domain(store, "other", "example.com", "")

    assert settings.theme_domains(store, "amr-query") == ["zenodo.org"]
    assert settings.all_theme_domains(store) == {"amr-query": ["zenodo.org"], "other": ["example.com"]}

    settings.remove_domain(store, "amr-query", "zenodo.org")
    assert settings.theme_domains(store, "amr-query") == []


def test_archiving_a_theme_drops_its_domains(store):
    settings.allow_domain(store, "amr-query", "zenodo.org", "")
    settings.allow_domain(store, "amr-query", "github.com", "")
    settings.drop_theme(store, "amr-query")
    assert settings.theme_domains(store, "amr-query") == []


def test_parse_connect_requests():
    text = "取れなかった。\n🔒 接続: zenodo.org（CASTELLA の特徴量を落とすため）\n🔒 接続: huggingface.co (重み)"
    assert settings.parse_connect_requests(text) == [
        ("zenodo.org", "CASTELLA の特徴量を落とすため"),
        ("huggingface.co", "重み"),
    ]


def test_parse_connect_requests_ignores_unsafe_names():
    """ボタンから足せるのは、ぴったりのドメイン名だけ。まとめての許可や IP、パスは受け付けない。"""
    text = "\n".join([
        "🔒 接続: *.github.com（全部）",
        "🔒 接続: 10.0.0.1（社内）",
        "🔒 接続: https://zenodo.org/records（URL）",
        "🔒 接続: localhost（手元）",
    ])
    assert settings.parse_connect_requests(text) == []


def test_valid_domain():
    assert settings.valid_domain("zenodo.org")
    assert settings.valid_domain("objects.githubusercontent.com")
    assert not settings.valid_domain("*.github.com")
    assert settings.valid_domain("*.github.com", allow_wildcard=True)
    assert not settings.valid_domain("*", allow_wildcard=True)
    assert not settings.valid_domain("zenodo.org/records")


def test_domain_request_is_resolved_once(store):
    req_id = settings.add_request(store, "C1", "10.1", "amr-query", "zenodo.org", "特徴量")
    req = settings.get_request(store, req_id)
    assert (req["domain"], req["status"]) == ("zenodo.org", "pending")

    assert settings.resolve_request(store, req_id, "allowed") is True
    assert settings.resolve_request(store, req_id, "denied") is False  # 2回押しても1回分
    assert settings.get_request(store, req_id)["status"] == "allowed"


def test_schedule_time_can_be_overridden(config, store):
    config = replace(config, schedule=ScheduleConfig(daily="08:00"), maintenance=MaintenanceConfig(time="22:00"))
    assert settings.schedule_time(config, store, "daily") == "08:00"
    assert settings.schedule_time(config, store, "maintenance") == "22:00"

    settings.set_schedule(store, "daily", "07:30", True)
    assert settings.schedule_time(config, store, "daily") == "07:30"

    settings.set_schedule(store, "daily", "07:30", False)  # 止めても時刻は覚えておく
    assert settings.schedule_time(config, store, "daily") == ""
    assert settings.schedule_setting(config, store, "daily") == ("07:30", False)


def test_maintenance_disabled_in_config_stays_off(config, store):
    config = replace(config, maintenance=MaintenanceConfig(enabled=False, time="22:00"))
    assert settings.schedule_time(config, store, "maintenance") == ""


def test_home_profile_override_changes_only_named_agent(config, store):
    settings.set_agent_profile(store, "course", "codex", "gpt-5.6-terra", "high")
    assert settings.agent_profile(config, store, "course").provider == "codex"
    assert settings.agent_profile(config, store, "course").model == "gpt-5.6-terra"
    assert settings.agent_profile(config, store, "work").provider == "claude"


def test_effective_profile_uses_its_default_recipe_before_a_home_override(config, store):
    from kei_agent.config import AgentProfile, ModelRecipe

    config = replace(config,
                     agent_profiles={"course": AgentProfile(provider="codex", default_recipe="standard")},
                     model_recipes={"standard": ModelRecipe("codex", "gpt-standard", "medium")})
    profile = settings.agent_profile(config, store, "course")
    assert (profile.provider, profile.model, profile.reasoning_effort) == ("codex", "gpt-standard", "medium")


def test_home_profile_override_is_detectable(config, store):
    assert not settings.has_agent_profile_override(store, "research")
    settings.set_agent_profile(store, "research", "codex", "gpt-home", "high")
    assert settings.has_agent_profile_override(store, "research")


def test_changing_provider_clears_the_previous_provider_model(config, store):
    settings.set_agent_profile(store, "research", "codex", "gpt-5.6-terra", "high")
    settings.set_agent_provider(store, "research", "claude")

    profile = settings.agent_profile(config, store, "research")
    assert (profile.provider, profile.model) == ("claude", "")


def test_resetting_a_profile_returns_to_the_config_recipe(config, store):
    from kei_agent.config import AgentProfile, ModelRecipe

    config = replace(config,
                     agent_profiles={"research": AgentProfile(provider="codex", default_recipe="standard")},
                     model_recipes={"standard": ModelRecipe("codex", "gpt-standard", "high")})
    settings.set_agent_profile(store, "research", "claude", "custom", "low")
    settings.clear_agent_profile(store, "research")

    profile = settings.agent_profile(config, store, "research")
    assert (profile.provider, profile.model, profile.reasoning_effort) == ("codex", "gpt-standard", "high")


@pytest.mark.parametrize("provider, model, effort", [
    ("unknown", "", "high"), ("codex", "x" * 121, "high"), ("codex", "", "maximum"),
])
def test_home_profile_override_rejects_invalid_values(config, store, provider, model, effort):
    with pytest.raises(ValueError):
        settings.set_agent_profile(store, "course", provider, model, effort)
