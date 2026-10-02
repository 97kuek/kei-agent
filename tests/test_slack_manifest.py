"""Slack App の manifest（`kei-agent manifest`。段階4の②）。オンにしたモジュールのスラッシュコマンドを足す。"""

from dataclasses import replace
from pathlib import Path

import pytest
from fakes import make_home

from kei_agent.configuration.config import load_config
from kei_agent.framework import modules
from kei_agent.operations import cli, slack_manifest

REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path, agents=None):
    """agents は担当の表でオンにするモジュール。None なら表を置かない（組み込みを全部使う）。"""
    return load_config(env={"KEI_AGENT_HOME": str(make_home(tmp_path, agents=agents))})


def test_the_repository_manifest_is_the_generated_one(tmp_path):
    """slack/manifest.yaml は、組み込みのモジュールを全部オンにしたときの出力と同じ（手で直してずれないように）。"""
    generated = slack_manifest.render(_config(tmp_path))
    assert (REPO / "slack" / "manifest.yaml").read_text(encoding="utf-8") == generated, \
        "kei-agent manifest の出力で slack/manifest.yaml を書き直してください"


def test_slash_commands_follow_the_modules_that_are_on(tmp_path):
    everything = slack_manifest.build(_config(tmp_path))
    toggl, = everything["features"]["slash_commands"]
    assert toggl == {"command": "/toggl", "description": "時間記録を始める・止める", "usage_hint": "[start|stop]",
                     "should_escape": False}
    assert "commands" in everything["oauth_config"]["scopes"]["bot"]

    without = slack_manifest.build(_config(tmp_path, ["research"]))
    assert "slash_commands" not in without["features"]
    assert "commands" not in without["oauth_config"]["scopes"]["bot"]


def test_a_users_module_brings_its_command(tmp_path):
    home = tmp_path / "home"
    folder = home / "modules" / "stamp"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text('api = 1\nname = "stamp"\n[slash_commands]\nstamp = "スタンプを押す"\n',
                                        encoding="utf-8")
    (folder / "module.py").write_text("class Module:\n    def __init__(self, core):\n        pass\n\n"
                                      "    async def on_slash_command(self, name, body):\n        return ''\n",
                                      encoding="utf-8")
    config = _config(tmp_path, ["stamp"])
    command, = slack_manifest.build(config, name="わたしの助手")["features"]["slash_commands"]
    assert command == {"command": "/stamp", "description": "スタンプを押す", "should_escape": False}
    assert '"わたしの助手"' in slack_manifest.render(config, name="わたしの助手")


@pytest.mark.parametrize("value", ['{ usage_hint = "[x]" }', '{ description = "説明", hint = "x" }', "1"])
def test_slash_commands_are_checked(tmp_path, value):
    folder = tmp_path / "stamp"
    folder.mkdir()
    (folder / "module.toml").write_text(f'api = 1\nname = "stamp"\n[slash_commands]\nstamp = {value}\n',
                                        encoding="utf-8")
    (folder / "module.py").write_text("class Module:\n    pass\n", encoding="utf-8")
    with pytest.raises(modules.ModuleError):
        modules.load_spec(folder)


def test_yaml_quotes_every_string():
    text = slack_manifest.to_yaml({"a": {"b": "x: #y", "c": True, "d": ["p", {"q": "r", "s": False}], "e": []}})
    assert text == ('a:\n  b: "x: #y"\n  c: true\n  d:\n    - "p"\n    - q: "r"\n      s: false\n  e: []')


def test_the_command_prints_the_manifest(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    monkeypatch.setattr(slack_manifest, "load_config", lambda: replace(config, modules=("time",)))
    with pytest.raises(SystemExit) as done:
        cli.main(["manifest", "--name", "Kei"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("# Slack App") and 'name: "Kei"' in out and '"/toggl"' in out
