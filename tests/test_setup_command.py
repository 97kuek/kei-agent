"""はじめの設定（`kei-agent setup`。段階4の④）。答えから設定・プロフィール・秘密情報を作り、もうあるものは書き換えない。"""

import stat
import tomllib

import pytest

from kei_agent.configuration.config import EXAMPLE_CONFIG, load_config
from kei_agent.operations import cli, doctor, setup_command
from kei_agent.operations.setup_command import Asker

BOT = "xoxb-1-" + "secretbot"
APP = "xapp-1-" + "secretapp"
PAGE = "0123456789abcdef0123456789abcdef"


class Script:
    """質問の言葉 → 答え。順に聞かれた質問を残す（秘密情報は secret のほうで答える）。"""

    def __init__(self, answers=None, secrets=None):
        self.answers = list(answers or [])
        self.secrets = dict(secrets or {})
        self.asked = []

    def ask(self, prompt):
        self.asked.append(prompt)
        for i, (words, answer) in enumerate(self.answers):
            if words in prompt:
                del self.answers[i]
                return answer
        return ""

    def secret(self, prompt):
        self.asked.append(prompt)
        name = prompt.split("（")[0].strip()
        values = self.secrets.get(name, "")
        if isinstance(values, list):
            return values.pop(0) if values else ""
        return values

    @property
    def asker(self):
        return Asker(ask=self.ask, secret=self.secret)


def _run(tmp_path, script, *, installer=None, found=("claude",)):
    home = tmp_path / "home"
    calls = []
    code = setup_command.run(
        script.asker, {"KEI_AGENT_HOME": str(home)}, which=lambda name: f"/bin/{name}" if name in found else None,
        installer=installer or (lambda name, remove: calls.append((name, remove)) or True),
        check=lambda env: [doctor.Finding(doctor.WARN, "設定", "点検した")])
    return code, home, calls


def test_a_first_setup_writes_the_profile_config_and_secrets(tmp_path, capsys):
    script = Script(
        answers=[("話し方", "一人称は「僕」。短く"), ("呼び方", "山田さん"), ("所属", "○○大学"),
                 ("外すモジュール", "voice work workdev"), ("研究テーマ", "~/study"), ("共通ホーム", f"https://www.notion.so/Home-{PAGE}?pvs=4"),
                 ("登録して起動しますか", "y")],
        secrets={"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP, "KEI_AGENT_ALLOWED_USER_ID": "U123",
                 "NOTION_TOKEN": "ntn_1", "MOODLE_ICS_URL": "https://moodle.example/ics"})
    code, home, calls = _run(tmp_path, script)
    out = capsys.readouterr().out
    assert code == 0

    profile = (home / "profile.md").read_text(encoding="utf-8")
    assert "- 一人称は「僕」。短く\n- 依頼者の呼び方: 山田さん" in profile and "- 所属: ○○大学" in profile
    assert "研究: （例" not in profile and "## 話し方" in profile

    config = load_config(env={"KEI_AGENT_HOME": str(home)})
    assert config.modules == ("course", "daily", "improve", "knowledge", "notion", "research", "time")
    assert config.research_root.name == "study" and config.notion.hub_home == PAGE and config.notion.research_home == ""
    # モジュール・チャンネル・AI は担当の表に書き、config.toml には残さない（説明のコメントも）
    assert config.agents_table.name == "agents.csv"
    assert config.agent_profiles["research"].provider == "claude" and config.agent_profiles["router"].provider == "claude"
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "modules =" not in text and "[agents" not in text and "[channels]" not in text
    assert "provider は App Home" not in text and "\n\n\n" not in text

    common = home / "secrets" / "kei-agent.zsh"
    assert stat.S_IMODE(common.stat().st_mode) == 0o600 and stat.S_IMODE(common.parent.stat().st_mode) == 0o700
    written = common.read_text(encoding="utf-8")
    assert f"export SLACK_BOT_TOKEN='{BOT}'" in written and "# export TOGGL_API_TOKEN=''" in written
    assert doctor.assigned(common)["KEI_AGENT_A2A_TOKEN"] and doctor.assigned(common)["KEI_AGENT_NOTION_GATEWAY_TOKEN"]
    # プロセスだけの鍵は、そのファイルに。何も入れなかったもの（研究の S2_API_KEY）はファイルを作らない
    assert "MOODLE_ICS_URL=" in (home / "secrets" / "kei-agent-course.zsh").read_text(encoding="utf-8")
    assert not (home / "secrets" / "kei-agent-research.zsh").exists()

    # 常駐は、ゲートウェイ → 担当 → 本体の順
    assert calls == [("notion", False), ("course", False), ("knowledge", False), ("research", False), ("", False)]
    assert BOT not in out and APP not in out and "ntn_1" not in out    # 秘密情報の値は画面に出さない
    assert "点検した" in out and "agents.csv" in out and "#course: 大学" in out
    assert "engine 列に、担当ごと" not in out    # AI は選んであるので、このあとやることには出さない


def test_files_that_exist_are_left_alone(tmp_path, capsys):
    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True)
    (home / "config.toml").write_text("handoff_after_turns = 5\n", encoding="utf-8")
    (home / "agents.csv").write_text("module,enabled,channels,engine,model,effort\nresearch,true,,claude,,\n", encoding="utf-8")
    (home / "profile.md").write_text("# わたしのプロフィール\n", encoding="utf-8")
    (home / "secrets" / "kei-agent.zsh").write_text("export SLACK_BOT_TOKEN='x'\n", encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in home.rglob("*") if path.is_file()}
    script = Script(answers=[("登録して起動しますか", "y")])
    code, _, calls = _run(tmp_path, script)
    out = capsys.readouterr().out
    assert code == 0 and {path: path.read_text(encoding="utf-8") for path in before} == before
    assert out.count("もうある: ") == 4 and not any("話し方" in q or "外すモジュール" in q for q in script.asked)
    # 要る鍵がそろっていないので、常駐は登録しない（手順だけ出す）
    assert calls == [] and "要る鍵がそろっていない" in out and "deploy/install.sh research、deploy/install.sh" in out


def test_answers_are_checked_before_anything_is_written(tmp_path, capsys):
    script = Script(
        answers=[("外すモジュール", "weather"), ("外すモジュール", "voice"), ("共通ホーム", "ホーム"), ("登録して起動しますか", "n")],
        secrets={"SLACK_BOT_TOKEN": [APP, "xoxb-it's", BOT], "SLACK_APP_TOKEN": APP, "KEI_AGENT_ALLOWED_USER_ID": "U1",
                 "NOTION_TOKEN": "ntn_1"})
    code, home, calls = _run(tmp_path, script, found=())
    out = capsys.readouterr().out
    assert code == 0 and calls == []
    assert "知らないモジュール: weather" in out
    assert "ページの URL か、32文字の ID" in out and "xoxb- で始まっていない" in out and "' は使えない" in out
    assert "claude も codex も見つからない" in out and "登録しなかった" in out
    assert f"export SLACK_BOT_TOKEN='{BOT}'" in (home / "secrets" / "kei-agent.zsh").read_text(encoding="utf-8")


def test_a_module_that_needs_another_cannot_be_kept_without_it(tmp_path, capsys):
    folder = tmp_path / "home" / "modules" / "stamp"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text('api = 1\nname = "stamp"\nlabel = "スタンプ"\n[depends]\nrequires = ["notion"]\n',
                                        encoding="utf-8")
    script = Script(answers=[("外すモジュール", "notion"), ("外すモジュール", "notion stamp")])
    code, home, _ = _run(tmp_path, script)
    out = capsys.readouterr().out
    assert code == 0 and "スタンプ（stamp）には notion が要る" in out and "（あなたのモジュール）" in out
    assert "notion" not in load_config(env={"KEI_AGENT_HOME": str(home)}).modules


def test_stopping_halfway_keeps_what_was_written(tmp_path, capsys):
    answers = iter(["", "", "", "", ""])

    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    code = setup_command.run(Asker(ask=ask, secret=ask), {"KEI_AGENT_HOME": str(tmp_path / "home")},
                             installer=lambda name, remove: pytest.fail("登録しない"), check=lambda env: [])
    assert code == 130 and "中断した" in capsys.readouterr().out
    assert (tmp_path / "home" / "profile.md").is_file() and not (tmp_path / "home" / "config.toml").exists()


@pytest.mark.parametrize(("value", "found"), [
    (PAGE, PAGE),
    ("01234567-89ab-cdef-0123-456789abcdef", PAGE),
    (f"https://www.notion.so/workspace/Cafe-Deadbeef-{PAGE}?pvs=4", PAGE),
    (f"https://www.notion.so/{PAGE.upper()}#abc", PAGE),
    ("https://www.notion.so/ホーム", ""),
    ("abc", ""),
])
def test_a_page_id_comes_from_the_url_or_the_id(value, found):
    assert setup_command.page_id(value) == found


def test_the_profile_drops_what_was_not_answered():
    example = setup_command.EXAMPLE_PROFILE.read_text(encoding="utf-8")
    text = setup_command.profile_text(example, "", "", {})
    assert "## 依頼者について" not in text and "- 一人称は「私」。です・ます調で、短く" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_the_example_config_must_be_rewritable():
    with pytest.raises(setup_command.ConfigError):
        setup_command.set_value("[notion]\n", "notion", "hub_home", PAGE)
    text = setup_command.set_value('[notion]\nhub_home = ""  # 共通ホーム\n', "notion", "hub_home", PAGE)
    assert text == f'[notion]\nhub_home = "{PAGE}"  # 共通ホーム\n'
    data = tomllib.loads(setup_command.config_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), {("notion", "hub_home"): PAGE}))
    assert data["notion"]["hub_home"] == PAGE
    assert not {"modules", "channels", "agents", "research_root", "course_root"} & set(data)


def test_the_command_dispatches(monkeypatch):
    monkeypatch.setattr(setup_command, "main", lambda argv: 4)
    with pytest.raises(SystemExit) as done:
        cli.main(["setup"])
    assert done.value.code == 4
