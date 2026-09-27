"""モジュールの定義（module.toml）と、設定の modules（docs/extensibility.md）。"""

import pytest

from kei_agent import home, modules, settings
from kei_agent.config import ConfigError, load_config
from kei_agent.schedule import task_names

WEATHER = '''api = 1
name = "weather"
label = "天気"
[schedules.weather]
label = "天気と電車"
default = "06:30"
'''
# 定期処理だけのモジュールの動き（module.py）
SCHEDULE_ONLY = '''class Module:
    def __init__(self, core):
        self.core = core

    async def run_schedule(self, name, day):
        return {"status": "done"}
'''


def _module(root, name, text, code=None):
    (root / name).mkdir(parents=True)
    (root / name / "module.toml").write_text(text, encoding="utf-8")
    if code is not None:
        (root / name / "module.py").write_text(code, encoding="utf-8")
    return root / name


def _config(tmp_path, text=""):
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    (home_dir / "config.toml").write_text(text, encoding="utf-8")
    return home_dir


def test_the_knowledge_module_is_described_by_its_definition():
    spec = modules.builtin()["knowledge"]
    assert (spec.label, spec.port, spec.channels) == ("知識", 8792, {"knowledge": ("knowledge",)})
    assert [s.name for s in spec.schedules] == ["literature", "reading"]
    offline = {u.name for u in spec.actor.use_cases if u.offline}
    assert offline == {"knowledge_pick", "knowledge_summary"} and spec.actor.default_use_case == "knowledge_answer"


@pytest.mark.parametrize(("text", "message"), [
    ('api = 2\nname = "x"\n', "枠の版が合いません"),
    ('api = 1\nname = "y"\n', "フォルダの名前"),
    ('api = 1\nname = "x"\ncolor = "red"\n', "知らないキー"),
    ('api = 1\nname = "x"\n[use_cases.a]\nclaude = { model = "m" }\n', "[actor]"),
    ('api = 1\nname = "x"\n[process]\nport = 80\n', "1024"),
    ('api = 1\nname = "x"\n[schedules.a]\ndefault = "7:00"\n', "HH:MM"),
    ('api = 1\nname = "x"\n[actor]\nprompt = "x.md"\n[use_cases.a]\nclaude = { effort = "low" }\n', "model"),
])
def test_a_broken_definition_says_what_is_wrong(tmp_path, text, message):
    with pytest.raises(modules.ModuleError, match=message.replace("[", r"\[").replace("]", r"\]")):
        modules.load_spec(_module(tmp_path, "x", text))


def test_own_modules_come_from_the_user_folder_and_must_not_collide(tmp_path):
    home_dir = _config(tmp_path)
    _module(home_dir / "modules", "weather", WEATHER, SCHEDULE_ONLY)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert "weather" in modules.known() and not modules.known()["weather"].builtin
    assert config.modules == ("course", "knowledge", "work")   # 知っていても、設定に書くまではオンにしない（組み込みだけ）

    _module(home_dir / "modules", "knowledge", 'api = 1\nname = "knowledge"\n')
    with pytest.raises(ConfigError, match="組み込みのモジュール「knowledge」と同じ名前"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_two_modules_cannot_share_a_schedule(tmp_path):
    home_dir = _config(tmp_path)
    _module(home_dir / "modules", "news", 'api = 1\nname = "news"\n[schedules.reading]\ndefault = "07:00"\n',
            SCHEDULE_ONLY)
    with pytest.raises(ConfigError, match="定期処理「reading」がぶつかっています"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_enabled_modules_bring_their_channels_schedules_actors_and_address(tmp_path):
    home_dir = _config(tmp_path, 'modules = ["knowledge", "weather"]\n')
    _module(home_dir / "modules", "weather", WEATHER, SCHEDULE_ONLY)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert config.modules == ("knowledge", "weather")
    assert config.module_channels == {"knowledge": ("knowledge",)}
    assert config.a2a.agents["knowledge"] == "http://127.0.0.1:8792"       # 書かなければ module.toml の番地
    assert task_names(config) == ("night", "literature", "reading", "weather", "daily", "review", "maintenance")
    assert settings.schedule_time(config, _store(config), "weather") == "06:30"
    assert settings.schedule_label(config, "weather") == "天気と電車"
    assert home.agent_labels(config)["knowledge"] == "知識" and "weather" not in home.agent_labels(config)


def test_turning_a_module_off_removes_what_it_brings(tmp_path):
    config = load_config(env={"KEI_AGENT_HOME": str(_config(tmp_path, "modules = []\n"))})
    assert config.modules == () and config.module_channels == {} and "knowledge" not in config.a2a.agents
    assert task_names(config) == ("night", "daily", "review", "maintenance")
    assert "knowledge" not in home.agent_labels(config)


def test_modules_in_the_config_must_exist_and_bring_what_they_require(tmp_path):
    with pytest.raises(ConfigError, match="知らないモジュール"):
        load_config(env={"KEI_AGENT_HOME": str(_config(tmp_path, 'modules = ["nothing"]\n'))})
    home_dir = _config(tmp_path, 'modules = ["digest"]\n')
    _module(home_dir / "modules", "digest", 'api = 1\nname = "digest"\n[depends]\nrequires = ["knowledge"]\n')
    with pytest.raises(ConfigError, match="knowledge が要ります"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_a_module_can_only_pick_models_from_the_core_list(tmp_path):
    home_dir = _config(tmp_path, 'modules = ["cheap"]\n')
    folder = _module(home_dir / "modules", "cheap", 'api = 1\nname = "cheap"\n[actor]\nprompt = "cheap.md"\n'
                                                    '[use_cases.cheap_answer]\nclaude = { model = "claude-2" }\n')
    (folder / "cheap.md").write_text("# 安い担当\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="claude のモデル claude-2 は使えません"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def _store(config):
    from kei_agent.store import Store
    return Store(config.db_path)


# 動き（module.py）

def test_a_module_without_code_cannot_have_channels_or_schedules(tmp_path):
    with pytest.raises(modules.ModuleError, match="module.py"):
        modules.load_spec(_module(tmp_path, "weather", WEATHER))
    with pytest.raises(modules.ModuleError, match="module.py"):
        modules.load_spec(_module(tmp_path, "memo", 'api = 1\nname = "memo"\n[channels]\nmemo = ["memo"]\n'))
    # 実行役だけのモジュール（ほかのモジュールから使うもの）は、module.py が無くてよい
    spec = modules.load_spec(_module(tmp_path, "helper", 'api = 1\nname = "helper"\n'))
    assert modules.load_code(spec) is None


@pytest.mark.parametrize(("text", "code", "message"), [
    (WEATHER, "VALUE = 1\n", "class Module がありません"),
    (WEATHER, "class Module:\n    pass\n", "run_schedule"),
    ('api = 1\nname = "weather"\n[channels]\nweather = ["weather"]\n', "class Module:\n    pass\n", "on_message"),
])
def test_code_without_the_hooks_it_needs_is_refused_at_startup(tmp_path, text, code, message):
    spec = modules.load_spec(_module(tmp_path, "weather", text, code))
    with pytest.raises(modules.ModuleError, match=message):
        modules.load_code(spec)


def test_code_can_read_files_next_to_it_and_is_reloaded_from_a_new_place(tmp_path):
    """module.py は同じフォルダのファイルを `from . import` で読める。同じ名前でも、場所が変われば読み直す。"""
    first = _module(tmp_path / "a", "weather", WEATHER, "from . import texts\n\n" + SCHEDULE_ONLY
                    + "    label = texts.LABEL\n")
    (first / "texts.py").write_text('LABEL = "晴れ"\n', encoding="utf-8")
    assert modules.load_code(modules.load_spec(first)).label == "晴れ"

    second = _module(tmp_path / "b", "weather", WEATHER, SCHEDULE_ONLY + '    label = "雨"\n')
    assert modules.load_code(modules.load_spec(second)).label == "雨"


def test_builtin_module_code_only_imports_the_windows():
    """組み込みのモジュールも、利用者のモジュールと同じく、窓口だけでコアに触れる。

    本体側の module.py は kei_agent.api、担当プロセス側（agent.py と、そこから読むファイル）は kei_agent_a2a.api。
    """
    import ast

    for path in modules.BUILTIN_DIR.glob("*/*.py"):
        window = "kei_agent.api" if path.name == modules.CODE_FILE else "kei_agent_a2a.api"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imported += [node.module for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module]
        assert [name for name in imported if name.startswith("kei_agent") and name != window] == [], path


# 担当プロセス（agent.py）

AGENT_CODE = '''from kei_agent_a2a.api import AgentSkill, SkillExecutor

SKILLS = [AgentSkill(id="forecast", name="天気", description="明日の天気", tags=["weather"])]


class Executor(SkillExecutor):
    async def handle(self, updater, metadata, text):
        await self.done(updater, "晴れ")
'''


def _agent_module(root, code=AGENT_CODE):
    folder = _module(root, "weather", 'api = 1\nname = "weather"\nlabel = "天気"\ndescription = "天気を調べる"\n'
                                      '[process]\nport = 8800\n')
    if code is not None:
        (folder / "agent.py").write_text(code, encoding="utf-8")
    return folder


def test_a_process_needs_its_agent_code_and_an_actor_needs_its_prompt(tmp_path):
    with pytest.raises(modules.ModuleError, match="agent.py"):
        modules.load_spec(_agent_module(tmp_path / "a", code=None))
    folder = _module(tmp_path / "b", "helper", 'api = 1\nname = "helper"\n[actor]\nprompt = "helper.md"\n'
                                              '[use_cases.helper_answer]\nclaude = { model = "claude-sonnet-5" }\n')
    with pytest.raises(modules.ModuleError, match="helper.md"):
        modules.load_spec(folder)
    (folder / "helper.md").write_text("# 手伝い\n", encoding="utf-8")
    assert modules.load_spec(folder).actor.prompt == "helper.md"


def test_agent_code_without_skills_or_an_executor_is_refused(tmp_path):
    spec = modules.load_spec(_agent_module(tmp_path, code="VALUE = 1\n"))
    with pytest.raises(modules.ModuleError, match="SKILLS"):
        modules.load_agent(spec)


def test_one_command_starts_any_module_process(tmp_path, monkeypatch):
    """共通の起動コマンドは、module.toml の番地と agent.py から担当を作る（src/ と pyproject.toml は触らない）。"""
    pytest.importorskip("a2a", reason="担当プロセスは a2a-sdk で動く")
    from kei_agent_a2a import launch, server

    home_dir = _config(tmp_path, 'modules = ["knowledge", "weather"]\n')
    _agent_module(home_dir / "modules")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home_dir))
    started = []
    monkeypatch.setattr(server, "serve", lambda name, build_card, build_executor, rpc_path, port, prefix:
                        started.append((name, build_card("http://127.0.0.1:8800"), port, prefix)))

    launch.main(["weather"])

    (name, card, port, prefix), = started
    assert (name, port, prefix) == ("天気エージェント", 8800, "KEI_AGENT_WEATHER")
    # 名刺の説明は、agent.py に DESCRIPTION が無ければ module.toml の description
    assert (card.name, card.description, [s.id for s in card.skills]) == ("Kei Agent（天気）", "天気を調べる", ["forecast"])
    _, build_executor = launch.parts(modules.known()["weather"])
    assert build_executor().agent == "weather"            # 制限の表とモデルの一覧を引く名前はモジュールの名前
    # 知らないもの、担当プロセスを持たないもの、設定の modules に無い（オフの）ものは起動しない
    _module(home_dir / "modules", "quiet", 'api = 1\nname = "quiet"\n')
    radar = _module(home_dir / "modules", "radar", 'api = 1\nname = "radar"\n[process]\nport = 8801\n')
    (radar / "agent.py").write_text(AGENT_CODE, encoding="utf-8")
    for name in ("nothing", "quiet", "radar"):
        with pytest.raises(SystemExit):
            launch.main([name])
    assert len(started) == 1


def test_a_module_can_ship_its_own_commands(tmp_path, monkeypatch):
    """手で動かすコマンド（setup など）は commands.py に置き、共通のコマンドから動かす（pyproject.toml を触らない）。"""
    pytest.importorskip("a2a", reason="共通のコマンドは kei_agent_a2a にある")
    from kei_agent_a2a import launch

    home_dir = _config(tmp_path)
    folder = _module(home_dir / "modules", "weather", 'api = 1\nname = "weather"\n')
    (folder / "commands.py").write_text(
        "RAN = []\n\n\ndef setup(argv):\n    RAN.append(argv)\n\n\nCOMMANDS = {\"setup\": setup}\n", encoding="utf-8")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home_dir))

    launch.main(["weather", "setup", "--apply"])

    code = __import__(f"{modules.package(modules.known()['weather'])}.commands", fromlist=["RAN"])
    assert code.RAN == [["--apply"]]
    with pytest.raises(SystemExit):
        launch.main(["weather", "nothing"])                 # 無いコマンド
    with pytest.raises(SystemExit):
        launch.main(["weather"])                            # 担当プロセスを持たない（コマンドだけのモジュール）


def test_a_command_gets_its_own_help(tmp_path, monkeypatch, capsys):
    """`kei-agent-module <名前> <コマンド> --help` は、共通のコマンドではなく、そのコマンドの説明を出す。"""
    pytest.importorskip("a2a", reason="共通のコマンドは kei_agent_a2a にある")
    from kei_agent_a2a import launch

    home_dir = _config(tmp_path)
    folder = _module(home_dir / "modules", "weather", 'api = 1\nname = "weather"\n')
    (folder / "commands.py").write_text(
        "import argparse\n\n\ndef forecast(argv):\n    argparse.ArgumentParser(prog='forecast', description='明日の天気')"
        ".parse_args(argv)\n\n\nCOMMANDS = {\"forecast\": forecast}\n", encoding="utf-8")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home_dir))
    with pytest.raises(SystemExit):
        launch.main(["weather", "forecast", "--help"])
    assert "明日の天気" in capsys.readouterr().out
