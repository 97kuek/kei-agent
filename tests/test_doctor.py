"""点検（`kei-agent doctor`。段階4の①）。読むだけで、困っているところと直し方を並べる。鍵の中身は出さない。"""

import time
from dataclasses import replace

import pytest

from kei_agent import cli, doctor
from kei_agent.config import AgentProfile
from kei_agent.doctor import ERROR, OK, WARN

SECRET = "xoxb-12345-" + "secretvalue"


def levels(findings, group=None):
    return [(f.level, f.text) for f in findings if group is None or f.group == group]


def _secrets(config, tmp_path, text, mode=0o600):
    directory = tmp_path / "secrets"
    directory.mkdir(exist_ok=True)
    path = directory / doctor.SECRETS_FILE
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return replace(config, secrets_dir=directory)


FULL = (f'export SLACK_BOT_TOKEN="{SECRET}"\nexport SLACK_APP_TOKEN="xapp-1"\nexport KEI_AGENT_ALLOWED_USER_ID="U1"\n'
        'export KEI_AGENT_A2A_TOKEN="abc"\nexport NOTION_TOKEN="ntn_1"\nexport KEI_AGENT_NOTION_GATEWAY_TOKEN="g"\n'
        'export TOGGL_API_TOKEN="t"\nexport TOGGL_ORGANIZATION_ID="1"\nexport TOGGL_WORKSPACE_ID="2"\n')


# 設定と秘密情報

def test_a_broken_config_is_the_only_finding(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("modules = [\"nothing\"]\n", encoding="utf-8")
    config, findings = doctor.check_config(env={"KEI_AGENT_HOME": str(home)})
    assert config is None and findings[0].level == ERROR and "nothing" in findings[0].text


def test_secrets_are_checked_by_name_only(config, tmp_path):
    ok = _secrets(config, tmp_path, FULL)
    assert levels(doctor.check_secrets(ok)) == [(OK, "kei-agent.zsh に要る鍵がそろっている")]

    # 例の書き方のまま・空・無いものは、足りない鍵として名前だけ出す（値は出さない）
    missing = _secrets(config, tmp_path, FULL.replace("xapp-1", "xapp-...").replace('"abc"', '""')
                       .replace("export NOTION_TOKEN", "# export NOTION_TOKEN"), mode=0o644)
    findings = doctor.check_secrets(missing)
    text = doctor.report(findings, verbose=True)
    assert "SLACK_APP_TOKEN、KEI_AGENT_A2A_TOKEN、NOTION_TOKEN" in text
    assert "ほかの人も読める" in text and SECRET not in text


def test_a_missing_secrets_file_and_partial_toggl(config, tmp_path):
    none = replace(config, secrets_dir=tmp_path / "nowhere")
    assert doctor.check_secrets(none)[0].level == ERROR
    partial = _secrets(config, tmp_path, FULL.replace('export TOGGL_WORKSPACE_ID="2"\n', ""))
    assert any("Toggl の鍵が一部だけ" in text for _, text in levels(doctor.check_secrets(partial)))


# AI

def test_actors_without_an_ai_and_missing_commands(config):
    config = replace(config, agent_profiles={**config.agent_profiles, "work": AgentProfile(provider=""),
                                             "course": AgentProfile(provider="codex")})
    findings = doctor.check_ai(config, which=lambda name: "/bin/claude" if name == "claude" else None)
    found = dict((text.split("（")[0], level) for level, text in levels(findings))
    assert found["AI が選ばれていない担当: 仕事"] == ERROR
    assert found["codex のコマンドが見つからない"] == ERROR and found["claude がある"] == OK


def test_the_ai_chosen_in_app_home_wins(config, store):
    store.set_setting("agent.work.provider", "codex")
    config = replace(config, agent_profiles={**config.agent_profiles, "work": AgentProfile(provider="")})
    assert doctor.chosen_providers(config)["work"] == "codex"


# 常駐と版

def test_processes_must_be_registered_and_running(config, tmp_path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    for name in ("assistant", "course", "knowledge"):
        (agents / f"com.kei-agent.{name}.plist").write_text("x")
    config = replace(config, modules=("course", "research"))
    states = {"com.kei-agent.assistant": "running", "com.kei-agent.course": "waiting"}
    findings = doctor.check_launchd(config, agents, lambda label: states.get(label, ""))
    found = {text.split("（")[0]: (level, f.hint) for (level, text), f in zip(levels(findings), findings, strict=True)}
    assert found["本体"][0] == OK
    assert found["大学"] == (ERROR, "~/Library/Logs/kei-agent/course-launchd.log を見る")
    assert found["研究"] == (ERROR, "deploy/install.sh research")
    assert found["オフのモジュールの常駐が残っている: 知識"] == (WARN, "deploy/install.sh knowledge remove")


async def test_versions_are_compared_with_the_repository(config):
    config = replace(config, modules=("course", "notion"),
                     a2a=replace(config.a2a, agents={"course": "http://127.0.0.1:8787"}))
    answers = {"8787": "old", "8791": ""}

    async def fetch(url):
        return next((found for port, found in answers.items() if port in url), "new")

    findings = await doctor.check_versions(config, fetch=fetch, disk="new")
    found = {text.split("（")[0]: level for level, text in levels(findings)}
    assert found["大学が古い版のまま"] == WARN and found["Notionが答えない"] == ERROR


# Notion・道具・ログ

def test_notion_homes_and_tools(config):
    config = replace(config, modules=("notion", "research", "improve"),
                     notion=replace(config.notion, hub_home="", course_home=""))
    notion = levels(doctor.check_notion(config))
    assert (WARN, "[notion] hub_home が空（Daily・振り返り・時間記録・予定カレンダーを Notion に残さない）") in notion
    tools = levels(doctor.check_tools(config, which=lambda name: "/bin/pueue" if name == "pueue" else None))
    assert tools == [(OK, "pueue がある（研究のジョブ）"), (WARN, "gh が見つからない（使うもの: 要望の GitHub issue）")]
    assert levels(doctor.check_notion(replace(config, modules=())))[0][0] == WARN


def test_recent_errors_in_the_log_are_counted(tmp_path):
    log = tmp_path / "kei-agent.log"
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 7200))
    log.write_text(f"{old},000 ERROR kei_agent: 前のもの\n{stamp},000 ERROR kei_agent: こわれた\n"
                   f"{stamp},000 INFO kei_agent: ふつう\n", encoding="utf-8")
    assert levels(doctor.check_logs(log, now)) == [(WARN, "最近1時間の ERROR が 1 件")]
    assert doctor.check_logs(tmp_path / "none.log", now) == []


# まとめと、コマンド

def test_the_report_hides_what_works_unless_asked():
    findings = [doctor.Finding(OK, "設定", "読めた"), doctor.Finding(ERROR, "AI", "選ばれていない", "App Home で選ぶ")]
    short = doctor.report(findings)
    assert "読めた" not in short and "❌ 選ばれていない" in short and "→ App Home で選ぶ" in short
    assert "問題 1 件、注意 0 件、うまくいっている 1 件" in short
    assert "✅ 読めた" in doctor.report(findings, verbose=True)


def test_the_command_runs_the_app_without_arguments(monkeypatch):
    from kei_agent import app

    ran = []
    monkeypatch.setattr(app, "main", lambda: ran.append("app"))
    cli.main([])
    assert ran == ["app"]

    monkeypatch.setattr(doctor, "main", lambda argv: 3)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["doctor"])
    assert stopped.value.code == 3
    with pytest.raises(SystemExit) as unknown:
        cli.main(["nothing"])
    assert unknown.value.code == 2


def test_doctor_never_creates_or_writes_the_state(config, tmp_path):
    """点検は読むだけ（状態のデータベースが無ければ作らず、あっても読み取りだけで開く）。"""
    fresh = replace(config, state_dir=tmp_path / "no-state")
    assert set(doctor.chosen_providers(fresh).values()) == {"claude"}      # config.toml の [agents] から
    assert not fresh.db_path.exists()
