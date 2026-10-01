"""モジュールのオン・オフ（`kei-agent module list / add / remove`。段階4の③）。"""

import pytest
from fakes import write_config

from kei_agent import agents_table, cli, module_command


def _home(tmp_path, text):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_config(home / "config.toml", text)
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


# 担当の表の書き換え

def _on(home):
    return agents_table.load(home / "agents.csv")["modules"]


def _change(home, name, on, calls=None, **kw):
    """`kei-agent module add/remove` を、その home で動かす。calls を渡すと launchd の登録を記録するだけにする。"""
    if calls is not None:
        kw["installer"] = lambda module, remove: calls.append((module, remove)) or True
    return module_command.change(name, on, env={"KEI_AGENT_HOME": str(home)}, **kw)


def test_adding_writes_a_checked_table_and_keeps_a_backup(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["research"]\n')
    before = (home / "agents.csv").read_text(encoding="utf-8")
    _stamp(home)
    calls = []
    assert _change(home, "stamp", True, calls) == 0
    out = capsys.readouterr().out
    assert _on(home) == ["research", "stamp"]
    assert (home / "agents.csv.bak").read_text(encoding="utf-8") == before
    assert calls == []                                  # 常駐を持たないモジュールは launchd に触らない
    assert "#stamp" in out and "kei-agent manifest" in out and "[stamp]）: color" in out
    assert not list(home.glob(".agents.csv.*"))         # 確かめるための一時ファイルは残さない


def test_without_a_table_it_starts_from_every_builtin_module(tmp_path, capsys):
    home = _home(tmp_path, "")                          # 表が無い = 組み込みを全部使う
    calls = []
    assert _change(home, "voice", False, calls) == 0
    names = _on(home)
    assert "voice" not in names and "research" in names and not (home / "agents.csv.bak").exists()
    out = capsys.readouterr().out
    assert calls == [("voice", True)] and "常駐（com.kei-agent.voice）を外した" in out and "を作った" in out


def test_the_launchd_step_when_it_was_not_changed(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["course", "work"]\n')
    assert _change(home, "research", True, launchd=False) == 0
    assert "deploy/install.sh research" in capsys.readouterr().out     # 登録は自分で
    assert set(_on(home)) == {"course", "research", "work"}
    assert _change(home, "voice", True, installer=lambda name, remove: False) == 0
    assert "deploy/install.sh voice" in capsys.readouterr().out         # 登録を変えられなかったとき
    assert "voice" in _on(home)


def test_nothing_is_written_when_the_new_config_would_break(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["notion", "stamp"]\n')
    _stamp(home, requires='"notion"')
    before = (home / "agents.csv").read_text(encoding="utf-8")
    assert _change(home, "notion", False) == 1
    assert "notion が要ります" in capsys.readouterr().out
    assert (home / "agents.csv").read_text(encoding="utf-8") == before and not (home / "agents.csv.bak").exists()


def test_a_broken_table_is_left_alone(tmp_path, capsys):
    home = _home(tmp_path, "")
    (home / "agents.csv").write_text("module,on\nresearch,true\n", encoding="utf-8")
    assert _change(home, "work", False) == 1
    assert "1行目" in capsys.readouterr().out
    assert (home / "agents.csv").read_text(encoding="utf-8") == "module,on\nresearch,true\n"


def test_dry_run_unknown_and_already_on(tmp_path, capsys):
    home = _home(tmp_path, 'modules = ["research"]\n')
    assert _change(home, "work", True, dry_run=True) == 0
    assert _on(home) == ["research"]
    assert _change(home, "nothing", True) == 1
    assert _change(home, "research", True) == 0
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
