"""モジュールの定義（module.toml）と、設定の modules（docs/extensibility.md）。"""

import pytest
from fakes import write_config

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
    write_config(home_dir / "config.toml", text)
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
    ('api = 1\nname = "x"\n[settings]\nPlace = "東京"\n', "設定の名前"),
    ('api = 1\nname = "x"\n[settings]\nsince = 2026-09-27\n', "既定の値"),
    ('api = 1\nname = "x"\n[secrets]\nApiKey = { description = "鍵" }\n', "環境変数の名前"),
    ('api = 1\nname = "x"\n[secrets]\nAPI_KEY = { required = true }\n', "description"),
    ('api = 1\nname = "x"\n[secrets]\nAPI_KEY = { description = "鍵", required = "yes" }\n', "true か false"),
    ('api = 1\nname = "x"\n[secrets]\nAPI_KEY = { description = "鍵", own_file = true }\n', "[process]"),
    ('api = 1\nname = "x"\n[secrets]\nSLACK_BOT_TOKEN = { description = "鍵" }\n', "本体の秘密情報"),
])
def test_a_broken_definition_says_what_is_wrong(tmp_path, text, message):
    with pytest.raises(modules.ModuleError, match=message.replace("[", r"\[").replace("]", r"\]")):
        modules.load_spec(_module(tmp_path, "x", text))


def test_secrets_come_from_the_core_and_the_modules_that_are_on():
    """要る秘密情報は、本体のものと、オンのモジュールの [secrets]（kei-agent setup が聞き、doctor が確かめる）。"""
    names = [(owner.name if owner else "", secret.name) for owner, secret in modules.secrets(["voice", "time"])]
    assert names[:4] == [("", "SLACK_BOT_TOKEN"), ("", "SLACK_APP_TOKEN"), ("", "KEI_AGENT_ALLOWED_USER_ID"),
                         ("", "KEI_AGENT_A2A_TOKEN")]
    assert names[4:] == [("voice", "OPENAI_API_KEY"), ("time", "TOGGL_API_TOKEN"), ("time", "TOGGL_ORGANIZATION_ID"),
                         ("time", "TOGGL_WORKSPACE_ID")]
    openai = modules.builtin()["voice"].secrets[0]
    # 声の鍵はマイクでの会話にだけ使う（喋って知らせるだけなら要らない）ので任意
    assert not openai.required and openai.own_file and not openai.generate
    gateway = next(s for s in modules.builtin()["notion"].secrets if s.name == "KEI_AGENT_NOTION_GATEWAY_TOKEN")
    assert gateway.generate and {s.group for s in modules.builtin()["time"].secrets} == {"Toggl"}


def test_own_modules_come_from_the_user_folder_and_must_not_collide(tmp_path):
    home_dir = _config(tmp_path)
    _module(home_dir / "modules", "weather", WEATHER, SCHEDULE_ONLY)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert "weather" in modules.known() and not modules.known()["weather"].builtin
    assert config.modules == ("course", "daily", "improve", "knowledge", "notion", "research", "time", "voice",
                              "work", "workdev")   # 知っていても、設定に書くまではオンにしない（組み込みだけ）

    _module(home_dir / "modules", "knowledge", 'api = 1\nname = "knowledge"\n')
    with pytest.raises(ConfigError, match="組み込みのモジュール「knowledge」と同じ名前"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_enabled_modules_bring_their_channels_schedules_actors_and_address(tmp_path):
    home_dir = _config(tmp_path, 'modules = ["knowledge", "weather"]\n')
    _module(home_dir / "modules", "weather", WEATHER, SCHEDULE_ONLY)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert config.modules == ("knowledge", "weather")
    assert config.module_channels == {"knowledge": ("knowledge",)}
    assert config.a2a.agents["knowledge"] == "http://127.0.0.1:8792"       # 書かなければ module.toml の番地
    # Daily と振り返りは、受け持つモジュール（daily）をオンにしたときだけ
    assert task_names(config) == ("night", "literature", "reading", "weather", "maintenance")
    assert settings.schedule_time(config, _store(config), "weather") == "06:30"
    assert settings.schedule_label(config, "weather") == "天気と電車"
    assert home.agent_labels(config)["knowledge"] == "知識" and "weather" not in home.agent_labels(config)


def test_turning_a_module_off_removes_what_it_brings(tmp_path):
    config = load_config(env={"KEI_AGENT_HOME": str(_config(tmp_path, "modules = []\n"))})
    assert config.modules == () and config.module_channels == {} and "knowledge" not in config.a2a.agents
    assert task_names(config) == ("night", "maintenance")
    assert "knowledge" not in home.agent_labels(config)


@pytest.mark.parametrize(("text", "name", "toml", "message"), [
    ('modules = ["nothing"]\n', None, None, "知らないモジュール"),
    ('modules = ["digest"]\n', "digest", '[depends]\nrequires = ["knowledge"]\n', "knowledge が要ります"),
    ("", "news", '[schedules.reading]\ndefault = "07:00"\n', "定期処理「reading」がぶつかっています"),
    ("", "paths", '[settings]\nroot = "~"\n', "本体の設定"),
    # モデルは本体の一覧からしか選べない
    ('modules = ["cheap"]\n', "cheap", '[actor]\nprompt = "cheap.md"\n'
     '[use_cases.cheap_answer]\nclaude = { model = "claude-2" }\n', "claude のモデル claude-2 は使えません"),
])
def test_a_config_whose_modules_do_not_fit_is_refused(tmp_path, text, name, toml, message):
    home_dir = _config(tmp_path, text)
    if name:
        folder = _module(home_dir / "modules", name, f'api = 1\nname = "{name}"\n{toml}',
                         SCHEDULE_ONLY if "[schedules" in toml else None)
        (folder / f"{name}.md").write_text("#\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


# 設定（module.toml の [settings] と、config.toml の [<名前>]）

WEATHER_SETTINGS = WEATHER + '[settings]\nplace = "東京"\nhours = 12\nalerts = []\n'


def test_a_module_declares_its_settings_and_the_config_can_change_them(tmp_path):
    """書ける項目と既定は module.toml の [settings]。利用者は config.toml の、モジュールの名前の表で変える。"""
    home_dir = _config(tmp_path, 'modules = ["weather"]\n\n[weather]\nplace = "早稲田"\n')
    _module(home_dir / "modules", "weather", WEATHER_SETTINGS, SCHEDULE_ONLY)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert config.settings("weather") == {"place": "早稲田", "hours": 12, "alerts": []}
    config.settings("weather")["alerts"].append("雷")       # 渡すのは写し。書き換えても設定は変わらない
    assert config.settings("weather")["alerts"] == [] and config.settings("knowledge") == {}


@pytest.mark.parametrize(("text", "message"), [
    ('[weather]\nplace = 1\n', r"\[weather\] place は、文字列で"),
    ('[weather]\nhours = true\n', r"\[weather\] hours は、整数で"),
    ('[weather]\ncolor = "red"\n', r"\[weather\] に知らないキー"),
    ('weather = "晴れ"\n', r"\[weather\] はテーブル"),
    ('[knowledge]\nplace = "東京"\n', "モジュール「knowledge」には設定がありません"),
    ('[wether]\nplace = "東京"\n', "一番外側 に知らないキーがあります: wether（使えるキー: .*weather"),
])
def test_module_settings_in_the_config_must_match_the_definition(tmp_path, text, message):
    """オフのモジュールの設定も確かめる（書き間違いを黙って無視しない）。"""
    home_dir = _config(tmp_path, text)
    _module(home_dir / "modules", "weather", WEATHER_SETTINGS, SCHEDULE_ONLY)
    with pytest.raises(ConfigError, match=message):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})


def test_settings_stay_when_the_module_is_turned_off(tmp_path):
    home_dir = _config(tmp_path, 'modules = []\n\n[weather]\nplace = "早稲田"\n\n[channels]\nwork = ["office"]\n')
    _module(home_dir / "modules", "weather", WEATHER_SETTINGS, SCHEDULE_ONLY)
    config = load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    assert config.modules == () and config.settings("weather")["place"] == "早稲田"
    # オフのモジュールのチャンネルの名前も、表に書いたまま残せる（使うのはオンのものだけ）
    assert config.module_channels == {}
    assert "work,false,office" in (home_dir / "agents.csv").read_text()


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

    for path in modules.BUILTIN_DIR.glob("*/**/*.py"):
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
    monkeypatch.setattr(server, "serve", lambda name, build_card, build_executor, rpc_path, port, prefix, build_app:
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


def test_a_module_process_can_be_a_service_instead_of_an_agent(tmp_path, monkeypatch):
    """A2A ではない常駐のプロセス（Notion のゲートウェイ）は kind = "service" と service.py の serve(config, port)。

    本体は担当としてつながない（[a2a.agents] に並べない）。起動は同じ共通のコマンド。
    """
    pytest.importorskip("a2a", reason="共通のコマンドは kei_agent_a2a にある")
    from kei_agent_a2a import launch

    home_dir = _config(tmp_path, 'modules = ["knowledge", "bridge"]\n')
    text = 'api = 1\nname = "bridge"\n[process]\nport = 8803\nkind = "service"\n'
    folder = _module(home_dir / "modules", "bridge", text)
    with pytest.raises(ConfigError, match="service.py"):
        load_config(env={"KEI_AGENT_HOME": str(home_dir)})
    (folder / "service.py").write_text("SERVED = []\n\n\ndef serve(config, port):\n    SERVED.append(port)\n    return 0\n",
                                       encoding="utf-8")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home_dir))
    config = load_config()
    assert modules.known()["bridge"].service and "bridge" not in config.a2a.agents
    with pytest.raises(SystemExit) as stopped:
        launch.main(["bridge"])
    assert stopped.value.code == 0 and modules.load_service(modules.known()["bridge"]).SERVED == [8803]


@pytest.mark.parametrize(("text", "code", "message"), [
    ('[process]\nport = 8803\nkind = "daemon"\n', None, "a2a / service"),
    ('[process]\nport = 8803\nkind = "service"\n', "def serve():\n    return 0\n", "serve\\(config, port\\)"),
])
def test_a_broken_service_is_refused(tmp_path, text, code, message):
    folder = _module(tmp_path, "bridge", 'api = 1\nname = "bridge"\n' + text)
    if code is None:
        with pytest.raises(modules.ModuleError, match=message):
            modules.load_spec(folder)
        return
    (folder / "service.py").write_text(code, encoding="utf-8")
    with pytest.raises(modules.ModuleError, match=message):
        modules.load_service(modules.load_spec(folder))


def test_a_module_can_ship_its_own_commands(tmp_path, monkeypatch, capsys):
    """手で動かすコマンド（setup など）は commands.py に置き、共通のコマンドから動かす（pyproject.toml を触らない）。
    `kei-agent-module <名前> <コマンド> --help` は、そのコマンドの説明を出す。"""
    pytest.importorskip("a2a", reason="共通のコマンドは kei_agent_a2a にある")
    from kei_agent_a2a import launch

    home_dir = _config(tmp_path)
    folder = _module(home_dir / "modules", "weather", 'api = 1\nname = "weather"\n')
    (folder / "commands.py").write_text(
        "import argparse\n\nRAN = []\n\n\ndef setup(argv):\n    RAN.append(argv)\n\n\n"
        "def forecast(argv):\n    argparse.ArgumentParser(prog='forecast', description='明日の天気')"
        ".parse_args(argv)\n\n\nCOMMANDS = {\"setup\": setup, \"forecast\": forecast}\n", encoding="utf-8")
    monkeypatch.setenv("KEI_AGENT_HOME", str(home_dir))

    launch.main(["weather", "setup", "--apply"])

    code = __import__(f"{modules.package(modules.known()['weather'])}.commands", fromlist=["RAN"])
    assert code.RAN == [["--apply"]]
    with pytest.raises(SystemExit):
        launch.main(["weather", "forecast", "--help"])
    assert "明日の天気" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        launch.main(["weather", "nothing"])                 # 無いコマンド
    with pytest.raises(SystemExit):
        launch.main(["weather"])                            # 担当プロセスを持たない（コマンドだけのモジュール）
