"""モジュールのオン・オフ（`kei-agent module list / add / remove`。段階4の③）。"""

import tomllib

import pytest

from kei_agent import cli, module_command


def _home(tmp_path, text):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")
    return home


def _stamp(home, requires=""):
    folder = home / "modules" / "stamp"
    folder.mkdir(parents=True)
    depends = f"[depends]\nrequires = [{requires}]\n" if requires else ""
    (folder / "module.toml").write_text(
        f'api = 1\nname = "stamp"\nlabel = "スタンプ"\n{depends}[channels]\nstamp = ["stamp"]\n'
        '[slash_commands]\nstamp = "スタンプを押す"\n[settings]\ncolor = "red"\n', encoding="utf-8")
    (folder / "module.py").write_text("class Module:\n    def __init__(self, core):\n        pass\n\n"
                                      "    async def on_message(self, req, skill='', params=None):\n        pass\n\n"
                                      "    async def on_slash_command(self, name, body):\n        return ''\n",
                                      encoding="utf-8")


# 設定の書き換え

def test_only_the_modules_line_changes():
    text = '# わたしの設定\nmodules = ["course", "work"]  # 使うもの\nhandoff_after_turns = 8\n\n[schedule]\ndaily = "08:00"\n'
    changed = module_command.with_modules(text, ["course"])
    assert changed == '# わたしの設定\nmodules = ["course"]\nhandoff_after_turns = 8\n\n[schedule]\ndaily = "08:00"\n'
    spread = 'modules = [\n  "course",\n  "work",\n]\nx = 1\n'
    assert module_command.with_modules(spread, ["work"]) == 'modules = ["work"]\nx = 1\n'


def test_a_missing_modules_line_goes_before_the_first_table():
    text = "# わたしの設定\nhandoff_after_turns = 8\n\n[schedule]\ndaily = \"08:00\"\n"
    changed = module_command.with_modules(text, ["course"])
    assert tomllib.loads(changed)["modules"] == ["course"] and tomllib.loads(changed)["schedule"]["daily"] == "08:00"
    assert changed.startswith("# わたしの設定\nhandoff_after_turns = 8\n\n# 使うモジュール")
    assert tomllib.loads(module_command.with_modules("x = 1", ["a"])) == {"x": 1, "modules": ["a"]}


def test_adding_writes_a_checked_config_and_keeps_a_backup(tmp_path, capsys):
    home = _home(tmp_path, '# わたしの設定\nmodules = ["research"]\n')
    _stamp(home)
    calls = []
    code = module_command.change("stamp", True, env={"KEI_AGENT_HOME": str(home)},
                                 installer=lambda name, remove: calls.append((name, remove)) or True)
    out = capsys.readouterr().out
    assert code == 0
    assert tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["modules"] == ["research", "stamp"]
    assert (home / "config.toml.bak").read_text(encoding="utf-8") == '# わたしの設定\nmodules = ["research"]\n'
    assert calls == []                                  # 常駐を持たないモジュールは launchd に触らない
    assert "#stamp" in out and "kei-agent manifest" in out and "[stamp]）: color" in out
    assert not list(home.glob(".config.toml.*"))        # 確かめるための一時ファイルは残さない


def test_removing_a_module_with_a_process_unregisters_it(tmp_path, capsys):
    home = _home(tmp_path, "")                          # modules を書いていない = 組み込みを全部使う
    calls = []
    assert module_command.change("voice", False, env={"KEI_AGENT_HOME": str(home)},
                                 installer=lambda name, remove: calls.append((name, remove)) or True) == 0
    names = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["modules"]
    assert "voice" not in names and "research" in names
    assert calls == [("voice", True)] and "常駐（com.kei-agent.voice）を外した" in capsys.readouterr().out


def test_the_order_and_the_launchd_step_when_it_was_not_changed(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["course", "work"]\n')
    env = {"KEI_AGENT_HOME": str(home)}
    assert module_command.change("research", True, env=env, launchd=False) == 0
    assert "deploy/install.sh research" in capsys.readouterr().out     # 登録は自分で
    assert module_command.current((home / "config.toml").read_text(encoding="utf-8")) == ["course", "research", "work"]
    (home / "config.toml").write_text('modules = ["work", "course"]\n', encoding="utf-8")
    assert module_command.change("voice", True, env=env, installer=lambda name, remove: False) == 0
    assert "deploy/install.sh voice" in capsys.readouterr().out         # 登録を変えられなかったとき
    assert module_command.current((home / "config.toml").read_text(encoding="utf-8")) == ["work", "course", "voice"]


def test_nothing_is_written_when_the_new_config_would_break(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["notion", "stamp"]\n')
    _stamp(home, requires='"notion"')
    before = (home / "config.toml").read_text(encoding="utf-8")
    assert module_command.change("notion", False, env={"KEI_AGENT_HOME": str(home)}) == 1
    assert "notion が要ります" in capsys.readouterr().out
    assert (home / "config.toml").read_text(encoding="utf-8") == before and not (home / "config.toml.bak").exists()


@pytest.mark.parametrize(("text", "said"), [
    ('modules = ["research"\n', "TOML として読めません"),
    ('modules = "research"\n', "名前の配列にしてください"),
    # 複数行の文字の中にある modules は、書き換えると別のものが変わるので断る
    ('note = """\nmodules = ["research"]\n"""\n', "うまく書き換えられない"),
])
def test_a_config_that_cannot_be_rewritten_is_left_alone(tmp_path, capsys, text, said):
    home = _home(tmp_path, text)
    assert module_command.change("work", False, env={"KEI_AGENT_HOME": str(home)}) == 1
    assert said in capsys.readouterr().out
    assert (home / "config.toml").read_text(encoding="utf-8") == text and not (home / "config.toml.bak").exists()


def test_dry_run_unknown_and_already_on(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["research"]\n')
    env = {"KEI_AGENT_HOME": str(home)}
    assert module_command.change("work", True, env=env, dry_run=True) == 0
    assert tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["modules"] == ["research"]
    assert module_command.change("nothing", True, env=env) == 1
    assert module_command.change("research", True, env=env) == 0
    out = capsys.readouterr().out
    assert "--dry-run なので" in out and "知らないモジュール" in out and "もうオン" in out


def test_the_list_shows_what_each_module_brings(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["research", "time"]\n')
    _stamp(home)
    assert module_command.list_modules(env={"KEI_AGENT_HOME": str(home)}) == 0
    out = capsys.readouterr().out
    assert "✅ research" in out and "・ stamp" in out and "スタンプ（あなたのモジュール）" in out
    assert "コマンド /toggl" in out and "常駐 8788" in out


def test_the_command_dispatches(monkeypatch):
    monkeypatch.setattr(module_command, "main", lambda argv: 5 if argv == ["list"] else 0)
    with pytest.raises(SystemExit) as done:
        cli.main(["module", "list"])
    assert done.value.code == 5
