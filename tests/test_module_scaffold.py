"""モジュールのひな形（`kei-agent module new`）と、そのテスト（`kei-agent module test`）。段階5の②。"""

import os
import socket

import pytest

from kei_agent.framework import modules
from kei_agent.operations import module_command, module_scaffold


def _env(tmp_path):
    return {"KEI_AGENT_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", "")}


@pytest.mark.parametrize(("ai", "process"), [(False, False), (True, False), (False, True), (True, True)])
def test_every_skeleton_loads_and_its_own_tests_pass(tmp_path, capsys, ai, process):
    if process:
        pytest.importorskip("a2a", reason="担当プロセスのひな形は a2a-sdk で動かす（uv run --group agents）")
    env = _env(tmp_path)
    assert module_scaffold.create("sample", ai=ai, process=process, label="見本", env=env) == 0
    out = capsys.readouterr().out
    folder = tmp_path / "home" / "modules" / "sample"
    spec = modules.load_spec(folder)
    assert (spec.label, spec.actor is not None, spec.port is not None) == ("見本", ai, process)
    assert (folder / "sample.md").is_file() == ai and (folder / "agent.py").is_file() == process
    if ai:
        # 組み込みの指示書と同じく、Slack に出す答えは印の中だけ（印が無いと、答えが空になる）
        prompt = (folder / "sample.md").read_text(encoding="utf-8")
        assert "<<kei-agent-final>>" in prompt and "<<kei-agent-final-end>>" in prompt
        assert "作業手順" in prompt and "Slack に出さない" in prompt
    assert spec.port is None or spec.port >= module_scaffold.FIRST_PORT
    assert "kei-agent module test sample" in out and "kei-agent module add sample" in out
    # 作ったひな形のテストが、そのまま通る（本物に触れない柵を付けた pytest を、別のプロセスで動かす）
    assert module_scaffold.run_tests("sample", ["-q", "-p", "no:cacheprovider"], env=env) == 0


def test_names_that_are_taken_or_wrong_are_refused(tmp_path, capsys):
    env = _env(tmp_path)
    assert module_scaffold.create("Memo", env=env) == 1
    assert module_scaffold.create("time", env=env) == 1                 # 組み込みと同じ名前
    (tmp_path / "home" / "modules" / "memo").mkdir(parents=True)       # 定義の無いフォルダがもうある
    assert module_scaffold.create("memo", env=env) == 1
    out = capsys.readouterr().out
    assert "英小文字で始め" in out and "もうある" in out and "フォルダがもうある" in out


def test_builtin_skeletons_go_to_the_repository_modules(tmp_path, monkeypatch, capsys):
    builtin_dir = tmp_path / "repo-modules"
    builtin_dir.mkdir()
    monkeypatch.setattr(modules, "BUILTIN_DIR", builtin_dir)
    assert module_scaffold.create("weather", builtin=True, env=_env(tmp_path)) == 0
    assert (builtin_dir / "weather" / "module.toml").is_file()
    assert not (tmp_path / "home" / "modules" / "weather").exists()


def test_a_process_gets_a_free_address(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
        monkeypatch.setattr(module_scaffold, "FIRST_PORT", port)
        # 手元で使われている番地と、ほかのモジュールの番地は飛ばす
        assert module_scaffold.free_port({port + 1}) >= port + 2


def test_tests_need_a_known_module_with_a_tests_folder(tmp_path, capsys):
    env = _env(tmp_path)
    assert module_scaffold.run_tests("nothing", [], env=env) == 1
    assert module_scaffold.run_tests("time", [], env=env) == 1          # 組み込みのテストはリポジトリの tests/
    out = capsys.readouterr().out
    assert "知らないモジュール" in out and "リポジトリの tests/" in out


def test_the_command_passes_the_rest_to_pytest(monkeypatch):
    seen = []
    monkeypatch.setattr(module_scaffold, "create", lambda name, **kw: seen.append(("new", name, kw)) or 0)
    monkeypatch.setattr(module_scaffold, "run_tests", lambda name, args: seen.append(("test", name, args)) or 0)
    assert module_command.main(["new", "memo", "--ai", "--label", "メモ"]) == 0
    assert module_command.main(["test", "memo", "-k", "answers", "-x"]) == 0
    assert seen == [("new", "memo", {"ai": True, "process": False, "builtin": False, "label": "メモ", "description": ""}),
                    ("test", "memo", ["-k", "answers", "-x"])]
    with pytest.raises(SystemExit):
        module_command.main(["list", "--unknown"])
