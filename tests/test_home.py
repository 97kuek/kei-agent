from dataclasses import replace

import pytest
from fakes import FakeAI, make_assistant

from kei_agent.conversation import home
from kei_agent.execution import runner
from kei_agent.storage import settings
from kei_agent.workspaces import themes


@pytest.fixture
def env(config, store, monkeypatch):
    monkeypatch.setattr(runner, "run_model", FakeAI())
    assistant, slack = make_assistant(config, store, {"C1": "vlm", "C5": "research-overview"})
    themes.ensure_workspace(themes.resolve(config, "vlm"))
    return assistant, slack


def _texts(view) -> str:
    out = []
    for b in view["blocks"]:
        out.append((b.get("text") or {}).get("text", ""))
        out += [e.get("text", "") for e in b.get("elements", []) if isinstance(e.get("text"), str)]
    return "\n".join(out)


def _published(slack):
    return [kw for name, kw in slack.calls if name == "views_publish"][-1]


def _action(action_id, value="", user="UME", **extra):
    return {"user": {"id": user}, "trigger_id": "trig", "actions": [{"action_id": action_id, "value": value, **extra}]}


def test_home_shows_neither_schedules_nor_connections(config, store):
    view = home.build_home(config, store, is_owner=True)
    text = _texts(view)
    # 通信の範囲は担当の線で決まるので、接続先の欄は無い
    assert "接続先" not in text
    # 定期処理の時刻とオンオフは schedules.csv だけで変える（App Home には出さない）
    assert "定期実行" not in text and not any(b.get("accessory", {}).get("type") == "timepicker" for b in view["blocks"])


def test_home_shows_agent_provider_controls(config, store):
    from kei_agent.configuration.config import AgentProfile, model_actors

    config = replace(config, agent_profiles={name: AgentProfile() for name in model_actors()})
    view = home.build_home(config, store, is_owner=True)
    course, = [b["accessory"] for b in view["blocks"]
               if b.get("accessory", {}).get("action_id") == "kei_agent_home_provider:course"]
    # 選んでいなければ、欄に「未選択」と出るだけ（説明文は置かない）
    assert "initial_option" not in course and course["placeholder"]["text"] == "未選択"
    controls = [block.get("accessory", {}) for block in view["blocks"]]
    assert {element.get("action_id") for element in controls} >= {
        "kei_agent_home_provider:research", "kei_agent_home_provider:course", "kei_agent_home_provider:work",
        "kei_agent_home_provider:router", "kei_agent_home_provider:improve",
        "kei_agent_home_provider:knowledge",
    }
    # 選べるのは AI だけ。モデルや深さは用途の表で決まり、ホームから上書きさせない
    action_ids = {element.get("action_id") for block in view["blocks"]
                  for element in [block.get("accessory", {}), *block.get("elements", [])]}
    assert not any(action_id.startswith("kei_agent_home_model:") for action_id in action_ids if action_id)
    assert not any(action_id.startswith("kei_agent_home_effort:") for action_id in action_ids if action_id)


def test_home_for_someone_else_changes_nothing(config, store):
    view = home.build_home(config, store, is_owner=False)
    assert "依頼者だけ" in _texts(view)
    assert not any(b.get("accessory") or b["type"] == "actions" for b in view["blocks"])


async def test_opening_or_refreshing_home_publishes_it(env):
    assistant, slack = env
    await assistant.on_home_opened({"type": "app_home_opened", "user": "UME", "tab": "home"})
    assert _published(slack)["user_id"] == "UME"
    slack.calls.clear()
    await assistant.on_home_action(_action(home.REFRESH_ACTION, "refresh"))
    assert _published(slack)["user_id"] == "UME"


async def test_someone_else_cannot_change_settings(env, store):
    assistant, slack = env
    voice = assistant.modules["voice"]
    await assistant.on_home_action(_action("kei_agent_home_module:voice:switches", user="USOMEONE",
                                           selected_options=[{"value": "notify"}]))
    assert not voice.is_on("notify") and not [name for name, _ in slack.calls if name == "views_publish"]


def _checked(*values):
    return [{"value": value} for value in values]


async def test_voice_checkboxes_open_and_close_the_microphone(env, store, monkeypatch):
    """「知らせる」「聞く（マイク）」は声のモジュールの項目。聞くを変えたときだけ、担当にマイクの開け閉めを伝える。"""
    assistant, slack = env
    told = []

    async def tell_agent(skill, payload):
        told.append((skill, payload))
        return True

    monkeypatch.setattr(assistant.cores["voice"], "tell_agent", tell_agent)
    voice, switches = assistant.modules["voice"], "kei_agent_home_module:voice:switches"
    await assistant.on_home_action(_action(switches, selected_options=_checked("notify", "listen")))
    assert voice.is_on("notify") and voice.is_on("listen") and told == [("notify", {"kind": "listen", "on": True})]
    await assistant.on_home_action(_action(switches, selected_options=_checked("notify")))
    assert voice.is_on("notify") and not voice.is_on("listen") and told[-1][1] == {"kind": "listen", "on": False}
    await assistant.on_home_action(_action(switches, selected_options=[]))
    assert not voice.is_on("notify") and len(told) == 2
    # チェックは「声」の見出しの下に、付いているものなしで出る
    blocks = _published(slack)["view"]["blocks"]
    title = next(n for n, block in enumerate(blocks) if (block.get("text") or {}).get("text") == "*声*")
    boxes, = blocks[title + 1]["elements"]
    assert boxes["action_id"] == switches and "initial_options" not in boxes


async def test_home_provider_action_changes_the_next_agent_run(env, config, store, monkeypatch):
    """App Home で選んだ AI で次の回が動き、モデルと深さは用途の表から選ぶ。"""
    assistant, _ = env
    await assistant.on_home_action(_action("kei_agent_home_provider:course", selected_option={"value": "codex"}))
    assert settings.agent_profile(assistant.config, store, "course").provider == "codex"
    await assistant.on_home_action(_action("kei_agent_home_provider:research", selected_option={"value": "codex"}))
    seen = {}

    async def fake_run(_config, request, _prompt, *_args, **_kwargs):
        seen.update(model=request.recipe.model, effort=request.recipe.reasoning_effort)
        return runner.RunResult(text="ok")

    monkeypatch.setattr(runner, "run_model", fake_run)
    await assistant.run_agent(themes.resolve(config, "vlm"), "実験計画を設計して")

    assert seen == {"model": "gpt-6-sol", "effort": "xhigh"}


# いま動いているもの


def test_home_shows_what_is_running(config, store):
    import time

    store.start_run("C1", "10.1", "vlm", "message")
    finished = store.start_run("C1", "10.0", "vlm", "message")
    store.end_run(finished, False, None)
    job = store.add_job("r1", "C1", "10.1", str(config.research_root / "vlm"), "sweep", "scripts/sweep.py",
                        status="queued")
    store.update_job(job.id, pueue_id=3, status="running")
    store.upsert_thread("C5", "20.1", "research-overview", None)
    store.set_awaiting("C5", "20.1", True)

    text = _texts(home.build_home(config, store, is_owner=True))

    assert "動いているもの" in text
    assert "#vlm" in text and "依頼" in text            # 動いている依頼
    assert "ジョブ 1「sweep」実行中" in text
    assert "#research-overview" in text and "返事待ち" in text
    assert str(int(time.time())) not in text            # 時刻ではなく経過時間で出す


def test_home_has_no_explanations_and_says_when_nothing_is_running(config, store):
    """見出しと操作だけ。説明の文（context）は「なし」などの状態だけにする。"""
    blocks = home.build_home(config, store, is_owner=True)["blocks"]
    assert blocks[1]["text"]["text"] == "*動いているもの*" and blocks[2]["elements"][0]["text"] == "なし"
    contexts = [e["text"] for b in blocks if b["type"] == "context" for e in b["elements"]]
    assert contexts == ["なし"]
    assert "config.toml" not in _texts({"blocks": blocks})

