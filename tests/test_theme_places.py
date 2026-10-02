"""研究テーマの置き場所（段階3の研究の③。docs/agents/research-agent.md の「テーマの作業場」）。

既存のフォルダやリポジトリをテーマに使う（themes.toml）、既存のリポジトリになるべく触らない、保護フォルダは
明示したときだけ、招いたときに置き場所を聞く。
"""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from fakes import FakeAI, FakeNotion, FakePueue, FakeSlack

from kei_agent.configuration.config import ConfigError, load_config
from kei_agent.conversation.assistant import Assistant
from kei_agent.execution import guard, runner
from kei_agent.execution.jobs import JobManager
from kei_agent.workspaces import themes
from kei_agent.workspaces.themes import ChannelKind, PlaceError


@pytest.fixture
def home(config, tmp_path):
    """利用者のフォルダ（themes.toml を置く場所）を持つ設定。"""
    user = tmp_path / "user"
    user.mkdir()
    return replace(config, user_dir=user)


def _repo(tmp_path, name="amr-repo", claude_md=None):
    folder = tmp_path / "src" / name
    (folder / ".git" / "info").mkdir(parents=True)
    if claude_md is not None:
        (folder / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return folder


def test_a_theme_can_use_an_existing_folder(home, tmp_path):
    folder = _repo(tmp_path)
    themes.save_place(home, "1-amr-query", folder)
    ws = themes.resolve(home, "1-amr-query")
    assert (ws.kind, ws.cwd, ws.external) == (ChannelKind.THEME, folder.resolve(), True)
    assert themes.all_themes(home) == {"amr-query": folder.resolve()}
    # 書いていないテーマは、今までどおり既定の場所
    other = themes.resolve(home, "vlm")
    assert (other.cwd, other.external) == (home.research_root / "vlm", False)


def test_saving_keeps_the_other_themes_and_the_file_is_read_again_when_it_changes(home, tmp_path):
    """名前は引用して書き、ほかのテーマは残す。本体も担当プロセスも、起動し直さずに新しい置き場所を使う。"""
    first, second, third = _repo(tmp_path, "one"), _repo(tmp_path, 'odd "name"'), _repo(tmp_path, "three")
    themes.save_place(home, "amr", first)
    assert themes.places(home)["amr"] == first.resolve()
    themes.save_place(home, "vlm", second)
    themes.save_place(home, "amr", second)
    assert themes.places(home) == {"amr": second.resolve(), "vlm": second.resolve()}
    path = home.user_dir / "themes.toml"
    path.write_text(f'amr = "{third}"\n', encoding="utf-8")
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    assert themes.places(home) == {"amr": third.resolve()}


def test_existing_folders_are_touched_as_little_as_possible(home, tmp_path):
    """CLAUDE.md はそのまま前提に使う。記録は .kei-agent/ にまとめて Git に入れない。inputs/ などは使うときに作る。"""
    folder = _repo(tmp_path, claude_md="# わたしのリポジトリ\n")
    themes.save_place(home, "amr", folder)
    ws = themes.resolve(home, "amr")
    themes.ensure_workspace(ws)
    themes.ensure_workspace(ws)
    assert (folder / "CLAUDE.md").read_text(encoding="utf-8") == "# わたしのリポジトリ\n"
    assert (folder / ".kei-agent").is_dir()
    assert not any((folder / sub).exists() for sub in themes.THEME_SUBDIRS)
    exclude = (folder / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count("/.kei-agent/") == 1                      # 何度用意しても一度だけ
    # CLAUDE.md が無いフォルダには、ひな形を置く
    bare = tmp_path / "bare"
    bare.mkdir()
    themes.save_place(home, "bare", bare)
    themes.ensure_workspace(themes.resolve(home, "bare"))
    assert "テーマ: bare" in (bare / "AGENTS.md").read_text(encoding="utf-8")


def test_places_that_cannot_be_used(home, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / "Documents" / "paper").mkdir(parents=True)
    monkeypatch.setattr(themes.Path, "home", lambda: fake_home)
    home.research_root.mkdir(parents=True, exist_ok=True)
    (home.research_root / "vlm").mkdir()
    for value, message in ((str(tmp_path / "nowhere"), "フォルダがありません"), ("src/amr", "~ か / から"),
                           (str(fake_home), "ホーム"), (str(home.research_root / "vlm"), "研究テーマの既定の置き場所"),
                           (str(home.repo_root), "Kei Agent 自身"), (str(home.user_dir), "設定の置き場所"),
                           (str(fake_home / "Documents" / "paper"), "allow_protected_folders")):
        with pytest.raises(PlaceError, match=message):
            themes.check_place(home, value)
    # 保護フォルダは、設定で許可したときだけ
    allowed = replace(home, allow_protected_folders=True)
    assert themes.check_place(allowed, str(fake_home / "Documents" / "paper")) == (fake_home / "Documents" / "paper")


def test_a_broken_themes_toml_stops_the_start(tmp_path):
    """書き間違いを黙って既定の場所に戻さない（起動のときに理由を出す）。"""
    user = tmp_path / "user"
    user.mkdir()
    (user / "config.toml").write_text("", encoding="utf-8")
    (user / "themes.toml").write_text(f'amr = "{tmp_path / "nowhere"}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="themes.toml の amr: フォルダがありません"):
        load_config(env={"KEI_AGENT_HOME": str(user)})
    (user / "config.toml").write_text("allow_protected_folders = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="allow_protected_folders"):
        load_config(env={"KEI_AGENT_HOME": str(user)})


def test_icloud_folders_get_a_warning(tmp_path, monkeypatch):
    from kei_agent.configuration import places
    monkeypatch.setattr(places, "ICLOUD_DRIVE", tmp_path / "Mobile Documents")
    (tmp_path / "Mobile Documents" / "work").mkdir(parents=True)
    assert "iCloud" in themes.icloud_warning(tmp_path / "Mobile Documents" / "work")
    assert themes.icloud_warning(tmp_path) == ""


def test_the_overview_reads_existing_theme_folders_too(home, tmp_path):
    folder = _repo(tmp_path)
    themes.save_place(home, "amr", folder)
    roots = guard.read_roots(home, themes.resolve(home, "0-overview"))
    assert folder.resolve() in roots and home.research_root in roots


def test_jobs_may_run_in_an_existing_theme_folder(home, tmp_path):
    pytest.importorskip("a2a", reason="担当プロセスは a2a-sdk で動く")
    from kei_agent_modules.research.agent import Executor

    folder = _repo(tmp_path)
    themes.save_place(home, "amr", folder)
    executor = Executor(home, pueue=object())
    assert executor._job_dir(str(folder)) == folder.resolve()
    with pytest.raises(ValueError, match="ジョブを動かしてよい場所ではありません"):
        executor._job_dir(str(tmp_path / "src"))


# 招いたときに置き場所を聞く

@pytest.fixture
def env(home, store, monkeypatch):
    slack = FakeSlack({"C1": "amr"})
    claude = FakeAI()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(home, store, slack, JobManager(home, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/")
    return assistant, slack, claude


def _buttons(slack):
    choice, = [kw for kw in slack.posted() if kw.get("blocks")]
    return {el["action_id"]: el for b in choice["blocks"] if b["type"] == "actions" for el in b["elements"]}


def _press(el, user="UME"):
    return {"user": {"id": user}, "trigger_id": "trig", "actions": [el],
            "container": {"channel_id": "C1", "message_ts": "5.5"}}


def _submit(folder, user="UME"):
    return {"user": {"id": user}, "view": {"private_metadata": json.dumps({"channel": "C1", "message_ts": "5.5"}),
                                           "state": {"values": {"folder": {"folder": {"value": str(folder)}}}}}}


async def test_joining_a_new_theme_asks_where_its_folder_is(env, home, tmp_path):
    assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
    buttons = _buttons(slack)
    assert set(buttons) == {"kei_agent_theme_place_default", "kei_agent_theme_place_existing"}
    assert not (home.research_root / "amr").exists() and assistant.notion.themes == {}

    await assistant.on_theme_place_action(_press(buttons["kei_agent_theme_place_existing"]))
    (name, view), = [(n, kw["view"]) for n, kw in slack.calls if n == "views_open"]
    assert view["callback_id"] == "kei_agent_theme_place_submit"

    # 使えない場所は、欄の下に理由を出す（画面は閉じない）
    assert "フォルダがありません" in (await assistant.on_theme_place_submit(_submit(tmp_path / "nowhere")))["folder"]
    folder = _repo(tmp_path, claude_md="# 前からある前提\n")
    assert await assistant.on_theme_place_submit(_submit(folder)) is None
    assert themes.places(home) == {"amr": folder.resolve()}
    assert assistant.notion.themes == {"amr": "https://example.slack.com/archives/C1"}
    text = slack.texts()[-1]
    assert str(folder.resolve()) in text and "Git には入れない" in text and "毎晩の保存はしない" in text
    assert "前からある `CLAUDE.md`" in text
    assert ("chat_update", {"channel": "C1", "ts": "5.5", "text": text.split("\n前からある")[0], "blocks": []}) in slack.calls

    # 依頼は、そのフォルダで動く
    await assistant.on_mention({"channel": "C1", "user": "UME", "ts": "10.1", "text": "<@UBOT> 図を作って"})
    while assistant.tasks:
        await asyncio.gather(*list(assistant.tasks), return_exceptions=True)
        await asyncio.sleep(0)
    assert claude.calls[-1]["cwd"] == folder.resolve()


async def test_choosing_the_default_place_creates_it(env, home):
    assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
    await assistant.on_theme_place_action(_press(_buttons(slack)["kei_agent_theme_place_default"]))
    assert (home.research_root / "amr" / "CLAUDE.md").exists() and themes.places(home) == {}
    assert "既定の場所" in slack.texts()[-1]


async def test_only_the_owner_chooses_the_place(env, home, tmp_path):
    assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C1"})
    await assistant.on_theme_place_action(_press(_buttons(slack)["kei_agent_theme_place_default"], user="USOMEONE"))
    assert not (home.research_root / "amr").exists()
    assert (await assistant.on_theme_place_submit(_submit(_repo(tmp_path), user="USOMEONE")))["folder"]
    assert themes.places(home) == {}
