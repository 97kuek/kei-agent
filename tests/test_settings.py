from dataclasses import replace

from fakes import write_agents, write_config, write_schedules

from kei_agent.configuration.config import MaintenanceConfig, ScheduleConfig
from kei_agent.storage import settings


def test_schedule_time_comes_from_the_config(config):
    """時刻は設定（schedules.csv から読んだもの）だけ。空文字と、止めた保守は「行わない」。"""
    config = replace(config, schedule=ScheduleConfig(daily="08:00", review=""), maintenance=MaintenanceConfig(time="22:00"))
    assert settings.schedule_time(config, "daily") == "08:00" and settings.schedule_time(config, "review") == ""
    assert settings.schedule_time(config, "maintenance") == "22:00"
    config = replace(config, maintenance=MaintenanceConfig(enabled=False, time="22:00"))
    assert settings.schedule_time(config, "maintenance") == ""


def test_the_provider_comes_only_from_the_table(config):
    """使える道具と連携は制限の表が決める。profile が持つのは provider・固定したモデル・アカウント・頭が選べる AI だけ。"""
    from dataclasses import replace as change

    from kei_agent.configuration.config import AgentProfile

    config = change(config, agent_profiles={**config.agent_profiles, "course": AgentProfile(provider="codex")})
    assert settings.selected_provider(config, "course") == "codex"
    assert settings.selected_provider(config, "work") == "claude"
    assert set(AgentProfile.__dataclass_fields__) == {"provider", "model", "effort", "claude_account", "codex_account",
                                                      "claude_email", "engines"}


def test_config_reads_the_knowledge_channel_and_reading_time(tmp_path):
    from kei_agent.configuration.config import load_config

    path = tmp_path / "config.toml"
    write_config(path, "")
    write_agents(tmp_path, ["research", {"module": "knowledge", "channels": "knowledge reading"}])
    write_schedules(tmp_path, [{"name": "reading", "time": "06:30"}])
    config = load_config(path, env={})
    assert config.module_channels["knowledge"] == ("knowledge", "reading")
    assert config.schedule.module_times["reading"] == "06:30"
    assert config.schedule.module_times["literature"] == "07:00"          # 書かなければ module.toml の既定
    assert "research-strategy" not in config.overview_channels


def test_the_sandbox_takes_only_its_known_keys(tmp_path):
    """[sandbox] に書けるのは allow_write と deny_read だけ（通信の範囲は担当の線で決まる）。"""
    import pytest

    from kei_agent.configuration.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    write_config(path, '[sandbox]\nallowed_domains = ["export.arxiv.org"]\nallow_write = ["~/.cache/uv"]\n')
    write_agents(tmp_path, ["research"])
    with pytest.raises(ConfigError, match="allowed_domains"):
        load_config(path, env={})
