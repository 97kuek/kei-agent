from dataclasses import replace

from fakes import write_agents, write_config, write_schedules

from kei_agent.configuration.config import MaintenanceConfig, ScheduleConfig
from kei_agent.storage import settings


def test_old_domain_tables_are_dropped(tmp_path):
    """使わなくなった接続先の表は、既存のデータベースからも消える。"""
    import sqlite3

    from kei_agent.storage.store import Store

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE theme_domains (theme TEXT, domain TEXT)")
        conn.execute("CREATE TABLE domain_requests (id INTEGER PRIMARY KEY)")
    store = Store(db)
    names = {r["name"] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not names & {"theme_domains", "domain_requests"}


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
    write_config(path, "")
    write_agents(tmp_path, ["research", {"module": "knowledge", "channels": "knowledge reading"}])
    write_schedules(tmp_path, [{"name": "reading", "time": "06:30"}])
    config = load_config(path, env={})
    assert config.module_channels["knowledge"] == ("knowledge", "reading")
    assert config.schedule.module_times["reading"] == "06:30"
    assert config.schedule.module_times["literature"] == "07:00"          # 書かなければ module.toml の既定
    assert "research-strategy" not in config.overview_channels


def test_an_old_allowed_domains_key_is_read_past(tmp_path):
    """[sandbox] allowed_domains が残った config.toml でも起動できる（通信の範囲は担当の線で決まる）。"""
    from kei_agent.configuration.config import load_config

    path = tmp_path / "config.toml"
    write_config(path, '[sandbox]\nallowed_domains = ["export.arxiv.org"]\nallow_write = ["~/.cache/uv"]\n')
    write_agents(tmp_path, ["research"])
    assert load_config(path, env={}).allow_write
