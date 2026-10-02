"""担当の表（agents.csv）: モジュールのオンオフ・チャンネル・AI の実行器とモデルを1か所で変える。"""

from pathlib import Path

import pytest

from kei_agent.configuration.agents_table import TableError, parse, with_enabled
from kei_agent.configuration.config import ConfigError, load_config
from kei_agent.execution.model_policy import resolve
from kei_agent.operations import module_command
from kei_agent.storage import settings
from kei_agent.storage.store import Store

HEADER = "module,enabled,channels,engine,model,effort\n"


def _home(tmp_path, table: str | None, config: str = ""):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(config, encoding="utf-8")
    if table is not None:
        (home / "agents.csv").write_text(table, encoding="utf-8")
    return home


def _load(home):
    return load_config(env={"KEI_AGENT_HOME": str(home)})


def test_the_table_decides_modules_channels_and_engines(tmp_path):
    home = _home(tmp_path, HEADER + (
        "router,true,,claude,,\n"
        "overview,true,overview research-overview,,,\n"
        "research,true,,codex,,\n"
        "course,TRUE,#course uni,claude,claude-sonnet-5,high\n"
        "work,false,work,claude,,\n"
        "improve,true,kei-agent,claude,,\n"
        "notion,true,,,,\n"))
    config = _load(home)
    assert config.agents_table.name == "agents.csv"
    # 表に無いモジュールと、false のモジュールはオフ
    assert config.modules == ("research", "course", "improve", "notion")
    assert config.module_channels["course"] == ("course", "uni")
    # 空欄のチャンネルは module.toml の既定
    assert config.module_channels["theme"] == ("*",)
    assert config.overview_channels == ("overview", "research-overview")
    assert config.improve_channels == ("kei-agent",)
    assert config.agent_profiles["research"].provider == "codex"
    assert config.agent_profiles["course"].model == "claude-sonnet-5"
    # 表に無い担当は未選択
    assert config.agent_profiles["daily"].provider == ""


def test_a_pinned_model_applies_to_every_use_case_except_manual_ones(tmp_path):
    home = _home(tmp_path, HEADER + "research,true,,claude,claude-opus-5,max\n")
    _load(home)
    for case in ("research_extract", "research_design"):
        recipe = resolve("research", "claude", case)
        assert (recipe.model, recipe.reasoning_effort) == ("claude-opus-5", "max")
    # 依頼者が明示したときだけの用途は、表のモデルにしない
    assert resolve("research", "claude", "manual_fable", manual=True).model == "claude-fable-5"
    # App Home で表と違う provider に一時的に切り替えたときは、用途ごとの選び分け
    assert resolve("research", "codex", "research_extract").model == "gpt-6-luna"
    # 空欄の担当は用途ごとの選び分けのまま
    assert resolve("course", "claude", "course_degree_plan").model == "claude-opus-5"


@pytest.mark.parametrize(("rows", "said"), [
    ("research,true,,claude,gpt-6-sol,\n", "claude では gpt-6-sol を使えません"),
    ("research,true,,claude,claude-fable-5,\n", "明示したときだけ"),
    ("research,true,,codex,gpt-6-sol,max\n", "effort"),
    ("research,true,,,claude-sonnet-5,\n", "engine も書いて"),
    ("research,true,,claude,,high\n", "model も書いて"),
    ("research,yes,,claude,,\n", "true か false"),
    ("research,true,,gemini,,\n", "claude、codex"),
    ("nothing,true,,,,\n", "知らないモジュール"),
    ("research,true,,,,\nresearch,false,,,,\n", "2つあります"),
    ("notion,true,,claude,,\n", "AI を使わない"),
    ("notion,true,notion,,,\n", "チャンネルを持ちません"),
    ("router,false,,claude,,\n", "オフにできません"),
    ("overview,true,,claude,,\n", "engine・model・effort を書けません"),
    ("course,true,course,uni,claude,,\n", "列が多すぎます"),
])
def test_mistakes_say_which_row_to_fix(tmp_path, rows, said):
    home = _home(tmp_path, HEADER + rows)
    with pytest.raises(ConfigError, match=said):
        _load(home)


def test_the_header_bom_and_comments(tmp_path):
    with pytest.raises(TableError, match="1行目"):
        parse("module,on\nresearch,true\n")
    # Excel が付ける BOM と、# で始まる行・空の行は読み飛ばす
    home = _home(tmp_path, None)
    (home / "agents.csv").write_bytes(("﻿" + HEADER + "# メモ, 列より多い, , , , , , ,\n\nknowledge,true,,codex,,\n").encode())
    assert _load(home).modules == ("knowledge",)
    (home / "agents.csv").write_bytes(HEADER.encode() + "knowledge,true,知識,,,\n".encode("shift_jis"))
    with pytest.raises(ConfigError, match="UTF-8"):
        _load(home)


def test_the_same_things_cannot_also_be_in_config_toml(tmp_path):
    home = _home(tmp_path, HEADER + "knowledge,true,,,,\n", 'modules = ["knowledge"]\n\n[agents.router]\nprovider = "claude"\n')
    with pytest.raises(ConfigError, match="modules・agents は、担当の表（agents.csv）に書いてください"):
        _load(home)
    # 表が無くても同じ（config.toml には書けない）
    (home / "agents.csv").unlink()
    with pytest.raises(ConfigError, match="agents.example.csv"):
        _load(home)


def test_without_a_table_every_builtin_module_is_on_and_no_ai_is_chosen(tmp_path):
    from kei_agent.framework import modules

    config = _load(_home(tmp_path, None))
    assert config.agents_table is None and set(config.modules) == set(modules.builtin())
    assert not any(p.provider for p in config.agent_profiles.values())


def test_app_home_switches_are_temporary(tmp_path):
    home = _home(tmp_path, HEADER + "knowledge,true,,codex,,\n")
    config = _load(home)
    store = Store(config.db_path)
    settings.set_agent_provider(store, "knowledge", "claude")
    assert settings.selected_provider(config, store, "knowledge") == "claude"
    assert settings.table_provider(config, "knowledge") == "codex"
    # 本体を起動し直すと、表の値に戻る
    assert settings.reset_agent_providers(store) == ["knowledge"]
    assert settings.selected_provider(config, store, "knowledge") == "codex"
    assert settings.reset_agent_providers(store) == []


def test_module_add_and_remove_rewrite_the_enabled_column(tmp_path, capsys):
    text = HEADER + "knowledge,true,,codex,,\nwork,false,work,claude,,\n"
    assert with_enabled(text, "work", True) == HEADER + "knowledge,true,,codex,,\nwork,true,work,claude,,\n"
    assert with_enabled(text, "daily", True).endswith("daily,true,,,,\n")
    home = _home(tmp_path, text)
    env = {"KEI_AGENT_HOME": str(home)}
    assert module_command.change("work", True, env=env, launchd=False) == 0
    assert (home / "agents.csv").read_text() == with_enabled(text, "work", True)
    assert (home / "agents.csv.bak").read_text() == text
    assert module_command.change("knowledge", False, env=env, launchd=False) == 0
    assert _load(home).modules == ("work",)
    assert "enabled = false" in capsys.readouterr().out


def test_app_home_shows_when_it_differs_from_the_table(tmp_path):
    from kei_agent.conversation.home import build_home

    home = _home(tmp_path, HEADER + "knowledge,true,,codex,,\n")
    config = _load(home)
    store = Store(config.db_path)
    text = str(build_home(config, store, True))
    assert "agents.csv では" not in text
    settings.set_agent_provider(store, "knowledge", "claude")
    assert "agents.csv では Codex（起動し直すと戻る）" in str(build_home(config, store, True))


def test_the_folder_column_decides_where_agents_work(tmp_path):
    """研究のテーマを置く場所・大学の作業場・ほかの担当の作業場は、表の folder 列で決まる。"""
    header = "module,enabled,channels,folder,engine,model,effort\n"
    home = _home(tmp_path, header + f"research,true,,{tmp_path}/r,claude,,\ncourse,true,,{tmp_path}/c,,,\n"
                                    f"knowledge,true,,{tmp_path}/k,,,\n")
    config = _load(home)
    assert (config.research_root, config.course_root) == ((tmp_path / "r").resolve(), (tmp_path / "c").resolve())
    assert config.module_workspace("knowledge") == (tmp_path / "k").resolve()
    # folder 列が無い表は、既定の場所
    (tmp_path / "old").mkdir()
    from kei_agent.configuration.config import DEFAULT_PATHS

    old = _load(_home(tmp_path / "old", HEADER + "research,true,,claude,,\n"))
    assert old.research_root == Path(DEFAULT_PATHS["research_root"]).expanduser().resolve()
    for rows, said in (("notion,true,,~/n,,,\n", "AI を使う担当の行だけ"), ("router,true,,~/r,claude,,\n", "AI を使う担当の行だけ")):
        (home / "agents.csv").write_text(header + rows)
        with pytest.raises(ConfigError, match=said):
            _load(home)
    # config.toml に書いてあれば、表に書くよう知らせる
    (home / "agents.csv").write_text(header)
    (home / "config.toml").write_text('research_root = "~/r"\n')
    with pytest.raises(ConfigError, match="research の行の folder 列に書いて"):
        _load(home)


def test_the_example_table_lists_every_builtin_module(tmp_path):
    from kei_agent.configuration.config import REPO_ROOT
    from kei_agent.framework import modules

    home = _home(tmp_path, (REPO_ROOT / "agents.example.csv").read_text(encoding="utf-8"))
    config = _load(home)
    assert set(config.modules) == set(modules.builtin())
    # 例は AI を選んでいない状態で始まる（config.example.toml と同じ）
    assert all(not p.provider and not p.model for p in config.agent_profiles.values())


def test_an_agent_runs_with_the_account_written_in_its_row(tmp_path):
    """大学は個人、仕事は会社のアカウント。表の claude_account・codex_account が、その担当の AI の環境に入る。"""
    from kei_agent.execution import runner
    from kei_agent.execution.agent_policy import policy_of

    header = "module,enabled,channels,engine,model,effort,claude_account,codex_account\n"
    config = _load(_home(tmp_path, header + f"work,true,,claude,,,{tmp_path}/claude-work,{tmp_path}/codex-work\n"
                                            "course,true,,claude,,,,\n"))
    base = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "common", "CLAUDE_CONFIG_DIR": "/default"}
    work = runner.build_env(config, base, "C1", "1.1", policy_of("work"))
    assert work["CLAUDE_CONFIG_DIR"] == str((tmp_path / "claude-work").resolve())
    assert work["CODEX_HOME"] == str((tmp_path / "codex-work").resolve())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in work           # 共通の鍵が残ると、アカウントのフォルダより先に使われる
    course = runner.build_env(config, base, "C1", "1.1", policy_of("course"))
    assert course["CLAUDE_CONFIG_DIR"] == "/default" and "CODEX_HOME" not in course
    with pytest.raises(ConfigError, match="AI を使う担当の行だけ"):
        _load(_home(tmp_path / "x", header + "notion,true,,,,,~/a,\n") if (tmp_path / "x").mkdir() is None else None)
