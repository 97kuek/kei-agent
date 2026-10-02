from dataclasses import replace

from fakes import write_config

from kei_agent.configuration.config import MaintenanceConfig, ScheduleConfig
from kei_agent.conversation import settings_actions
from kei_agent.execution import guard
from kei_agent.storage import settings


def test_domains_are_kept_per_theme(store):
    settings.allow_domain(store, "amr-query", "zenodo.org", "特徴量を落とすため")
    settings.allow_domain(store, "amr-query", "zenodo.org", "二度目")  # 同じものは1つにまとめる
    settings.allow_domain(store, "other", "example.com", "")

    assert settings.theme_domains(store, "amr-query") == ["zenodo.org"]
    assert settings.all_theme_domains(store) == {"amr-query": ["zenodo.org"], "other": ["example.com"]}

    settings.remove_domain(store, "amr-query", "zenodo.org")
    assert settings.theme_domains(store, "amr-query") == []
    # テーマを片づけたら、そのテーマの許可も消す
    settings.allow_domain(store, "amr-query", "zenodo.org", "")
    settings.allow_domain(store, "amr-query", "github.com", "")
    settings.drop_theme(store, "amr-query")
    assert settings.theme_domains(store, "amr-query") == []


def test_parse_connect_requests_takes_only_exact_domain_names():
    """ボタンから足せるのは、ぴったりのドメイン名だけ。まとめての許可や IP、パスは受け付けない。"""
    text = "\n".join([
        "取れなかった。",
        "🔒 接続: zenodo.org（CASTELLA の特徴量を落とすため）",
        "🔒 接続: *.github.com（全部）",
        "🔒 接続: 10.0.0.1（社内）",
        "🔒 接続: https://zenodo.org/records（URL）",
        "🔒 接続: localhost（手元）",
        "🔒 接続: huggingface.co (重み)",
    ])
    assert settings_actions.parse_connect_requests(text) == [
        ("zenodo.org", "CASTELLA の特徴量を落とすため"),
        ("huggingface.co", "重み"),
    ]


def test_valid_domain():
    assert guard.valid_domain("zenodo.org")
    assert guard.valid_domain("objects.githubusercontent.com")
    assert not guard.valid_domain("*.github.com")
    assert guard.valid_domain("*.github.com", allow_wildcard=True)
    assert not guard.valid_domain("*", allow_wildcard=True)
    assert not guard.valid_domain("zenodo.org/records")


def test_domain_request_is_resolved_once(store):
    req_id = settings.add_request(store, "C1", "10.1", "amr-query", "zenodo.org", "特徴量")
    req = settings.get_request(store, req_id)
    assert (req["domain"], req["status"]) == ("zenodo.org", "pending")

    assert settings.resolve_request(store, req_id, "allowed") is True
    assert settings.resolve_request(store, req_id, "denied") is False  # 2回押しても1回分
    assert settings.get_request(store, req_id)["status"] == "allowed"


def test_schedule_time_comes_from_the_config(config):
    """時刻は設定（schedules.csv から読んだもの）だけ。空文字と、止めた保守は「行わない」。"""
    config = replace(config, schedule=ScheduleConfig(daily="08:00", review=""), maintenance=MaintenanceConfig(time="22:00"))
    assert settings.schedule_time(config, "daily") == "08:00" and settings.schedule_time(config, "review") == ""
    assert settings.schedule_time(config, "maintenance") == "22:00"
    config = replace(config, maintenance=MaintenanceConfig(enabled=False, time="22:00"))
    assert settings.schedule_time(config, "maintenance") == ""


def test_home_provider_changes_only_the_named_agents_provider(config, store):
    """使える道具と連携は制限の表が決める。profile が持つのは provider と、担当の表で固定したモデルだけ。"""
    settings.set_agent_provider(store, "course", "codex")
    profile = settings.agent_profile(config, store, "course")
    assert profile.provider == "codex"
    assert set(profile.__dataclass_fields__) == {"provider", "model", "effort", "claude_account", "codex_account", "engines"}
    assert settings.agent_profile(config, store, "work").provider == "claude"


def test_config_reads_the_knowledge_channel_and_reading_time(tmp_path):
    from kei_agent.configuration.config import load_config

    path = tmp_path / "config.toml"
    write_config(path, '[channels]\nknowledge = ["knowledge", "reading"]\n\n[schedule]\nreading = "06:30"\n')
    config = load_config(path, env={})
    assert config.module_channels["knowledge"] == ("knowledge", "reading")
    assert config.schedule.module_times["reading"] == "06:30"
    assert config.schedule.module_times["literature"] == "07:00"          # 書かなければ module.toml の既定
    assert "research-strategy" not in config.overview_channels
