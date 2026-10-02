"""時間記録のモジュール（段階3の時間記録の②。modules/time/）。

`/toggl` と固定したカードで測り、Toggl と共通ホームの「時間記録」に送る。送れなかったものは見回りで送り直し、
Toggl のアプリで直接測った記録は定期処理で取り込む。記録はモジュールの記録（本体の表からは一度だけ写す）。
"""

import asyncio
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fakes import FakeAI, FakeHub, FakePueue, FakeSlack
from slack_sdk.errors import SlackApiError

from kei_agent.conversation.assistant import Assistant
from kei_agent.execution import runner
from kei_agent.execution.agents import Reply
from kei_agent.execution.jobs import JobManager
from kei_agent.framework import modules
from kei_agent.scheduling.timelog import TogglAmbiguousWrite, TogglError
from kei_agent.storage.notion import NotionError
from kei_agent.storage.records import Records
from kei_agent.storage.store import Store
from kei_agent.testing.kit import settle
from kei_agent_modules.time import commands, entries, importer
from kei_agent_modules.time.entries import Entries

START = "kei_agent_module:time:start"
STOP = "kei_agent_module:time:stop"


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "1-vlm", "C2": "2-course", "C3": "3-work", "C4": "2-linear-algebra",
                       "C9": "0-kei-agent"})
    monkeypatch.setattr(runner, "run_model", FakeAI())
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          team_url="https://example.slack.com/", hub=FakeHub())
    module = assistant.modules["time"]
    # Toggl の設定は、使うテストだけが入れる
    monkeypatch.setattr(sys.modules[type(module).__module__], "load_toggl", lambda: None)
    return assistant, module, slack


def use_toggl(monkeypatch, module, toggl):
    monkeypatch.setattr(sys.modules[type(module).__module__], "load_toggl", lambda: toggl)




def press(action_id, value="start", channel="C1", name="1-vlm", user="UME"):
    where = {"id": channel, "name": name} if name else {"id": channel}
    return {"user": {"id": user}, "trigger_id": "trig", "channel": where,
            "actions": [{"action_id": action_id, "value": value}]}


def toggl_command(text="", channel="C1", name="1-vlm", user="UME"):
    return {"user_id": user, "channel_id": channel, "channel_name": name, "text": text, "trigger_id": "trig"}


class RecordingToggl:
    def __init__(self, calls, fail=None):
        self.calls = calls
        self.fail = fail

    def record_completed(self, project, description, started_at, seconds):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("toggl", project, seconds))


async def measure(module, channel, theme, domain="research", minutes=25):
    """25分測って止めた記録（止めたあとの送信まで）。"""
    entry, _ = module.entries.start("UME", domain, channel, theme, started_at=time.time() - minutes * 60)
    stopped = module.entries.stop("UME", ended_at=entry.started_at + minutes * 60)
    await module._after_change(stopped=stopped, post_cards=False)
    return module.entries.entry(stopped.id)


# カード

async def test_the_card_starts_and_stops_one_timer(env):
    assistant, module, slack = env
    await assistant.module_action(press(START))
    entry = module.entries.active("UME")
    assert entry is not None and entry.description == "研究 / vlm" and entry.channel_name == "vlm"
    card = module.core.records.get("card", "C1")
    assert card["ts"] and card["buttons"] == "kei_agent_module:time:"
    posted = slack.posted()[-1]
    assert [el["action_id"] for el in posted["blocks"][1]["elements"]] == [STOP, "kei_agent_module:time:memo"]

    await assistant.module_action(press(STOP, entry.id))
    assert module.entries.active("UME") is None
    assert module.entries.entry(entry.id).ended_at is not None
    update = [kw for name, kw in slack.calls if name == "chat_update"][-1]
    assert update["ts"] == card["ts"] and update["blocks"][1]["elements"][0]["action_id"] == START


async def test_stale_stops_and_other_people_leave_the_timer_alone(env):
    assistant, module, slack = env
    await assistant.module_action(press(START, user="USOMEONE"))
    assert module.entries.active("USOMEONE") is None and slack.posted() == []
    await assistant.module_action(press(START))
    running = module.entries.active("UME")
    await assistant.module_action(press(STOP, "old-entry"))
    assert module.entries.active("UME") == running


@pytest.mark.parametrize(("by_card", "channel", "name", "domain", "description"), [
    # ボタンの body にチャンネル名がないときも、番号付きの名前で見分ける
    (True, "C3", "", "work", "仕事 / work"),
    # 非公開のチャンネルは privategroup と届くので、Slack に名前を聞く
    (False, "C3", "privategroup", "work", "仕事 / work"),
    # 授業ごとのチャンネル（pick_course に無いもの）は、チャンネルの名前で測る
    (False, "C4", "2-linear-algebra", "course", "大学 / linear-algebra"),
])
async def test_the_raw_channel_name_decides_the_domain(env, by_card, channel, name, domain, description):
    assistant, module, slack = env
    if by_card:
        await assistant.module_action(press(START, channel=channel, name=name))
    else:
        await assistant.module_slash("toggl", toggl_command(channel=channel, name=name))
    entry = module.entries.active("UME")
    assert (entry.domain, entry.description) == (domain, description)


async def test_a_deleted_card_is_posted_again(env):
    assistant, module, slack = env
    module.entries.set_card("C1", "gone.1", module.buttons)

    async def missing(**kw):
        raise RuntimeError("message_not_found")

    slack.chat_update = missing
    await assistant.module_action(press(START))
    assert module.entries.card("C1") not in ("", "gone.1")


async def test_the_memo_is_saved_from_its_view(env):
    assistant, module, slack = env
    await assistant.module_action(press(START))
    entry = module.entries.active("UME")
    await assistant.module_action(press("kei_agent_module:time:memo", entry.id))
    (_, opened), = [(n, kw) for n, kw in slack.calls if n == "views_open"]
    assert opened["view"]["callback_id"] == "kei_agent_module:time:memo"

    view = {"callback_id": "kei_agent_module:time:memo", "private_metadata": entry.id,
            "state": {"values": {"memo": {"text": {"value": "  図の直し  "}}}}}
    assert await assistant.module_view({"user": {"id": "UME"}, "view": view}) is None
    assert module.entries.entry(entry.id).memo == "図の直し"
    gone = {**view, "private_metadata": "nothing"}
    assert await assistant.module_view({"user": {"id": "UME"}, "view": gone}) == {"memo": "この記録はもうありません"}


# /toggl

async def test_toggl_toggles_and_switches_channels_without_posting_cards(env):
    assistant, module, slack = env
    started = await assistant.module_slash("toggl", toggl_command())
    first = module.entries.active("UME")
    assert first.description == "研究 / vlm" and "始めた" in started

    # 別のチャンネルで打つと、前のを止めてそこで測り始める
    switched = await assistant.module_slash("toggl", toggl_command(channel="C3", name="3-work"))
    await settle(assistant)
    now = module.entries.active("UME")
    assert (now.channel, now.domain) == ("C3", "work") and "研究 / vlm は止めた" in switched
    assert module.entries.entry(first.id).ended_at == now.started_at

    stopped = await assistant.module_slash("toggl", toggl_command(channel="C3", name="3-work"))
    await settle(assistant)
    assert module.entries.active("UME") is None and "止めた" in stopped
    # カードを置いていないチャンネルに、コマンドで新しいカードを投稿しない
    assert module.entries.card("C1") == "" and module.entries.card("C3") == "" and slack.posted() == []


async def test_toggl_start_and_stop_words(env):
    assistant, module, slack = env
    assert "計測していない" in await assistant.module_slash("toggl", toggl_command("stop"))
    await assistant.module_slash("toggl", toggl_command("開始"))
    first = module.entries.active("UME")
    # start は計測中でも止めずに、動いていることを伝えるだけ
    assert "計測中" in await assistant.module_slash("toggl", toggl_command("start"))
    assert module.entries.active("UME") == first
    await assistant.module_slash("toggl", toggl_command("停止"))
    assert module.entries.active("UME") is None
    assert "使い方" in await assistant.module_slash("toggl", toggl_command("pause"))


async def test_toggl_refuses_other_channels_and_people(env):
    assistant, module, slack = env
    assert "1-・2-・3-" in await assistant.module_slash("toggl", toggl_command(channel="C9", name="0-kei-agent"))
    assert "利用できません" in await assistant.module_slash("toggl", toggl_command(user="USOMEONE"))


async def test_the_course_channel_asks_which_course(env, monkeypatch):
    """科目を選ぶチャンネル（設定の pick_course）では、大学のモジュールに今学期の科目を聞いて選ばせる。"""
    assistant, module, slack = env
    asked = []
    reply = Reply.broken("つながらない")

    async def views_open(**kw):
        slack.calls.append(("views_open", kw))
        return {"view": {"id": "V1"}}

    async def ask_agent(skill, payload):
        asked.append(skill)
        return reply

    slack.views_open = views_open
    monkeypatch.setattr(assistant.cores["course"], "ask_agent", ask_agent)

    # 科目を読み出せないときは、選ぶ欄を出さずにそう伝える
    await assistant.module_action(press(START, channel="C2", name="2-course"))
    await settle(assistant)
    (_, update), = [(n, kw) for n, kw in slack.calls if n == "views_update"]
    assert "読み出せなかった" in str(update["view"]) and "submit" not in update["view"]

    reply = Reply(ok=True, data={"items": [{"id": "P1", "subject": "信号処理"}]})
    answer = await assistant.module_slash("toggl", toggl_command(channel="C2", name="2-course"))
    await settle(assistant)
    assert "科目" in answer and asked == ["list-current-courses"] * 2 and module.entries.active("UME") is None
    update = [kw for n, kw in slack.calls if n == "views_update"][-1]
    assert update["view_id"] == "V1" and "信号処理" in str(update["view"])
    option = update["view"]["blocks"][0]["element"]["options"][0]

    view = {"callback_id": "kei_agent_module:time:course", "private_metadata": "C2",
            "state": {"values": {"course": {"select": {"selected_option": option}}}}}
    assert await assistant.module_view({"user": {"id": "UME"}, "view": view}) is None
    await settle(assistant)
    entry = module.entries.active("UME")
    assert (entry.domain, entry.course_page_id, entry.label, entry.description) == (
        "course", "P1", "信号処理", "大学 / 信号処理")
    # 選び直しを求めるのは、選んだものが読めないときだけ
    broken = {**view, "state": {"values": {"course": {"select": {"selected_option": {"value": "x"}}}}}}
    assert await assistant.module_view({"user": {"id": "UME"}, "view": broken}) == {"course": "科目を選び直してね"}


# Toggl と共通ホームへの送信

async def test_every_domain_goes_to_toggl_then_to_the_hub(env, monkeypatch):
    assistant, module, slack = env
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls))
    module.entries.bind_course("C2", "course-page", "マルチメディア工学A")
    # カードのあるチャンネルなら、カードへのリンクも送る
    links, record_time = [], assistant.hub.record_time
    monkeypatch.setattr(assistant.hub, "record_time", lambda *args: (links.append(args[6]), record_time(*args)))
    module.entries.set_card("C1", "5.5", module.buttons)

    research = await measure(module, "C1", "vlm")
    course = await measure(module, "C2", "course", "course")
    work = await measure(module, "C3", "work", "work")

    assert calls == [("toggl", "研究 / vlm", 1500), ("toggl", "大学 / マルチメディア工学A", 1500),
                     ("toggl", "仕事 / work", 1500)]
    assert assistant.hub.recorded == [(research.id, "research", "vlm", 25, "Slack"),
                                      (course.id, "course", "マルチメディア工学A", 25, "Slack"),
                                      (work.id, "work", "work", 25, "Slack")]
    assert links == ["https://example.slack.com/archives/C1/p55", "", ""]
    for entry in (research, course, work):
        assert (entry.toggl_state, entry.notion_state) == ("done", "done")
    # 送り終えた記録は、しばらくしたら消える（Toggl と「時間記録」に残っている）
    row = assistant.store.module_record("time", "entry", research.id)
    assert row["expires_at"] == pytest.approx(time.time() + entries.KEEP_DAYS * 86400, abs=60)


async def test_without_toggl_the_hub_still_gets_the_time_with_the_card_link(env):
    assistant, module, slack = env
    await assistant.module_action(press(START))
    entry = module.entries.active("UME")
    await assistant.module_action(press(STOP, entry.id))

    sent = module.entries.entry(entry.id)
    assert (sent.toggl_state, sent.notion_state) == ("not_configured", "done")
    assert assistant.hub.recorded == [(entry.id, "research", "vlm", 1, "Slack")]


async def test_a_toggl_failure_is_kept_and_holds_back_the_hub(env, monkeypatch, caplog):
    assistant, module, slack = env
    use_toggl(monkeypatch, module, RecordingToggl([], fail=TogglError("POST /time-entries/bulk: 503")))
    entry = await measure(module, "C3", "work", "work")

    assert (entry.toggl_state, entry.notion_state) == ("pending", "pending")
    assert assistant.hub.recorded == [] and "503" in caplog.text
    assert assistant.store.module_record("time", "entry", entry.id)["expires_at"] is None

    use_toggl(monkeypatch, module, RecordingToggl([]))
    await module._look_around()
    assert module.entries.entry(entry.id).sent


async def test_an_unsure_toggl_write_waits_for_the_owner(env, monkeypatch):
    """送ったあとに切れたら、二重に入れないよう [Togglへ再送] を出して待つ。見回りでは送り直さない。"""
    assistant, module, slack = env
    calls = []
    use_toggl(monkeypatch, module, RecordingToggl(calls, fail=TogglAmbiguousWrite("POST: TimeoutError")))
    entry = await measure(module, "C1", "vlm")

    assert entry.toggl_state == "needs_review" and assistant.hub.recorded == []
    retry = slack.posted()[-1]["blocks"][1]["elements"][0]
    assert (retry["action_id"], retry["value"]) == ("kei_agent_module:time:retry", entry.id)

    use_toggl(monkeypatch, module, RecordingToggl(calls))
    await module._look_around()
    assert calls == []
    await assistant.module_action(press(retry["action_id"], entry.id))
    assert calls == [("toggl", "研究 / vlm", 1500)] and module.entries.entry(entry.id).sent


async def test_hub_trouble_waits_quietly_and_is_retried_by_the_look_around(env, monkeypatch):
    """共通ホームが使えない間・失敗した記録は保留にするだけで、毎回は知らせない。見回りで送り直す。"""
    assistant, module, slack = env
    hub = assistant.hub
    assistant.hub = None
    waiting = await measure(module, "C1", "vlm")
    await module._look_around()
    assert module.entries.entry(waiting.id).notion_state == "pending" and slack.posted() == []

    assistant.hub = hub
    failing = True
    record_time = hub.record_time

    def flaky(*args):
        if failing:
            raise NotionError("503")
        record_time(*args)

    monkeypatch.setattr(hub, "record_time", flaky)
    failed = await measure(module, "C1", "vlm")
    assert module.entries.entry(failed.id).notion_state == "pending"
    failing = False
    await module._look_around()
    assert {r[0] for r in hub.recorded} == {waiting.id, failed.id}
    assert module.entries.entry(failed.id).notion_state == "done"


async def test_the_look_around_runs_in_the_background_one_at_a_time(env, monkeypatch):
    """Toggl が遅くても、毎分の定期処理を待たせない。前の見回りが終わるまで、次は始めない。"""
    assistant, module, slack = env
    gate = asyncio.Event()
    runs = []

    async def slow():
        runs.append(1)
        await gate.wait()

    monkeypatch.setattr(module, "_redraw_cards", slow)
    await module.tick(datetime.now())
    await asyncio.sleep(0)
    await module.tick(datetime.now())
    await asyncio.sleep(0)
    assert runs == [1]
    gate.set()
    await settle(assistant)


# 前のボタンのカード

async def test_old_cards_get_the_new_buttons_once(env):
    """本体が置いたカード（ボタンの名前が前のもの）は、起動して最初の見回りで、今のボタンに描き直す。固定はそのまま。
    消されていたカードは置き直さずに忘れ、つながらなかったカードは次に起動したときにもう一度試す。"""
    assistant, module, slack = env
    module.core.records.put("card", "C1", {"channel": "C1", "ts": "11.1"})
    module.core.records.put("card", "C3", {"channel": "C3", "ts": "33.3"})
    entry, _ = module.entries.start("UME", "work", "C3", "work")

    await module.tick(datetime.now())
    await settle(assistant)
    updates = {kw["ts"]: kw for name, kw in slack.calls if name == "chat_update"}
    assert set(updates) == {"11.1", "33.3"}
    assert updates["11.1"]["blocks"][1]["elements"][0]["action_id"] == START
    assert updates["33.3"]["blocks"][1]["elements"][0]["value"] == entry.id       # 計測中のカードは計測中のまま
    assert {c["buttons"] for c in module.entries.cards()} == {module.buttons}

    await module.tick(datetime.now())
    await settle(assistant)
    assert len([n for n, _ in slack.calls if n == "chat_update"]) == 2

    module.core.records.put("card", "C2", {"channel": "C2", "ts": "22.2"})
    module.core.records.put("card", "C4", {"channel": "C4", "ts": "44.4"})

    async def update(**kw):
        if kw["channel"] == "C2":
            raise SlackApiError("message_not_found", {"ok": False, "error": "message_not_found"})
        raise ConnectionError("つながらない")

    slack.chat_update = update
    await module._redraw_cards()
    assert module.entries.card("C2") == "" and module.entries.card("C4") == "44.4"
    assert module.entries.card("C1") == "11.1" and slack.posted() == []


# 設定

def test_the_channel_prefixes_can_be_changed_and_are_checked(config, store):
    slack = FakeSlack({"C7": "5_lab"})
    custom = replace(config, module_settings={**config.module_settings,
                                              "time": {"prefixes": {"5_": "research"}, "pick_course": []}})
    module = Assistant(custom, store, slack, JobManager(custom, store, FakePueue()), "x", "UBOT").modules["time"]
    assert module.domain("5_lab") == "research" and module.domain("1-vlm") == ""

    with pytest.raises(ValueError, match="research / course / work"):
        entries.prefixes_of({"1-": "hobby"})
    with pytest.raises(ValueError, match="表にしてください"):
        entries.prefixes_of([])
    # 長い頭から見る
    assert entries.domain_of(entries.prefixes_of({"1": "work", "1-": "research"}), "1-vlm") == "research"


def test_the_toggl_import_runs_at_22_by_default():
    assert [(s.name, s.default) for s in modules.builtin()["time"].schedules] == [("toggl_import", "22:00")]


# Toggl で直接測った記録の取り込み（定期処理 toggl_import）

class FakeToggl:
    def __init__(self, entries_):
        self._entries = entries_
        self.asked = []

    def entries(self, since, until):
        self.asked.append((since, until))
        return self._entries


def toggl_entry(start, duration, project="研究/amr-query", **extra):
    """Toggl 2.0 の time-entries が返す1件（使う項目だけ）。"""
    e = {"start": start, "duration": duration, "type": "activity",
         "project": {"id": 7, "name": project} if project else None}
    return e | extra


class ImportHub:
    def __init__(self, known=()):
        self.known = set(known)
        self.recorded = []

    def time_ids_since(self, since):
        return set(self.known)

    def record_time(self, entry_id, domain, label, started_at, minutes, memo="", slack_url="", source="Slack"):
        self.recorded.append({"id": entry_id, "domain": domain, "label": label, "started_at": started_at,
                              "minutes": minutes, "memo": memo, "source": source})


def test_import_adds_only_marked_entries_measured_outside_slack():
    """Toggl のアプリで直接測った分だけを「toggl:<id>」で入れる。Slack から送った分と、入れ済みの分は飛ばす。"""
    slack_start = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    toggl = FakeToggl([
        # Slack から送った記録（Toggl 側の開始が数秒ずれても同じとみなす）
        toggl_entry((slack_start + timedelta(seconds=20)).isoformat(), 1500, project="研究 / vlm", id=1),
        # 開始が同じでも、長さが1分より大きくずれていれば別の記録
        toggl_entry(slack_start.isoformat(), 1500 + 120, project="研究/vlm", id=8),
        toggl_entry("2026-09-21T13:00:00Z", 3000, project="大学/データベース", id=2, description="過去問"),
        toggl_entry("2026-09-21T15:00:00Z", 600, project="アルバイト", id=3),        # 印がない
        toggl_entry("2026-09-21T16:00:00Z", -1, project="研究/vlm", id=4),         # 計測中
        toggl_entry("2026-09-21T17:00:00Z", 900, project="仕事/定例", id=5),        # 入れ済み
        toggl_entry("2026-09-21T18:00:00Z", 900, project="研究/vlm", id=6, type="break"),
        toggl_entry("2026-09-21T19:00:00Z", 30, project="仕事/定例", id=7, description="仕事/定例"),
    ])
    hub = ImportHub(known={"toggl:5"})

    result = importer.import_toggl(toggl, hub, [(slack_start.timestamp(), 1500.0)], date(2026, 9, 15),
                                   date(2026, 9, 21))

    assert toggl.asked == [(date(2026, 9, 15), date(2026, 9, 21))]
    assert [r["id"] for r in hub.recorded] == ["toggl:8", "toggl:2", "toggl:7"]
    first = hub.recorded[1]
    assert (first["domain"], first["label"], first["minutes"], first["memo"], first["source"]) == (
        "大学", "データベース", 50, "過去問", "Toggl")
    assert datetime.fromisoformat(first["started_at"]) == datetime(2026, 9, 21, 13, 0, tzinfo=UTC)
    # 説明がプロジェクト名と同じならメモにしない。1分に満たなくても1分として残す
    assert (hub.recorded[2]["memo"], hub.recorded[2]["minutes"]) == ("", 1)
    assert result == {"status": "done", "imported": 3, "own": 1, "known": 1, "unmarked": 1}


def test_split_project_needs_a_known_mark():
    assert importer.split_project("研究/amr-query") == ("研究", "amr-query")
    assert importer.split_project("大学/マルチメディア工学A") == ("大学", "マルチメディア工学A")
    assert importer.split_project("amr-query") is None       # 印がない
    assert importer.split_project("趣味/写真") is None        # 知らない印
    assert importer.split_project("研究/") is None            # 名前がない


async def test_the_schedule_imports_toggl_and_skips_slack_entries(env, monkeypatch):
    assistant, module, slack = env
    started = time.time() - 3600
    entry, _ = module.entries.start("UME", "research", "C1", "vlm", started_at=started)
    module.entries.stop("UME", ended_at=started + 1500)
    iso = datetime.fromtimestamp(started).astimezone().isoformat()
    use_toggl(monkeypatch, module, FakeToggl([
        {"id": 1, "start": iso, "duration": 1500, "project": {"name": "研究 / vlm"}},
        {"id": 2, "start": "2026-09-17T10:00:00+09:00", "duration": 600, "project": {"name": "仕事/定例"}}]))

    detail = await module.run_schedule("toggl_import", date.today().isoformat())

    assert assistant.hub.recorded == [("toggl:2", "仕事", "定例", 10, "Toggl")]
    assert (detail["imported"], detail["own"]) == (1, 1)


async def test_the_schedule_waits_for_the_time_db_and_reports_toggl_failures(env, monkeypatch):
    assistant, module, slack = env
    monkeypatch.setattr(sys.modules[type(module).__module__], "load_toggl",
                        lambda: pytest.fail("時間記録が無いのに Toggl を読んだ"))
    assistant.hub.has_time_db = False
    assert await module.run_schedule("toggl_import", "2026-09-18") == {"status": "skipped", "reason": "no_hub"}
    hub, assistant.hub = assistant.hub, None
    assert await module.run_schedule("toggl_import", "2026-09-18") == {"status": "skipped", "reason": "no_hub"}

    class Broken:
        def entries(self, since, until):
            raise TogglError("GET /time-entries: 503")

    hub.has_time_db, assistant.hub = True, hub
    use_toggl(monkeypatch, module, Broken())
    detail = await module.run_schedule("toggl_import", "2026-09-18")
    assert detail["status"] == "error" and "503" in detail["error"]


# Daily と振り返りの材料

async def test_the_material_shows_this_weeks_time(env):
    assistant, module, slack = env
    assistant.hub.minutes = {"研究": 90, "大学": 30}
    lines = await module.material(datetime(2026, 9, 18, 8, 0).timestamp())
    assert lines[1] == "## 時間（今週）"
    assert "- 人: 合計 2.0 時間（研究 1.5 時間、大学 0.5 時間）" in lines
    assert "- 時間記録（週ごとのグラフ）: https://www.notion.so/timedb" in lines

    assistant.hub.minutes = {}
    assert any("まだ記録がない" in line and "`研究/`・`大学/`・`仕事/`" in line
               for line in await module.material(time.time()))
    assistant.hub = None
    assert (await module.material(time.time()))[-1] == "- 人: 共通 Notion ホームが使えないので分からない"


# カードを置くコマンド（kei-agent-module time cards）

async def test_the_cards_command_posts_only_where_cards_are_missing(config, store):
    slack = FakeSlack({"C1": "1-vlm", "C2": "2-course", "C5": "0-overview", "C9": "0-kei-agent"})
    Entries(Records(store, "time")).set_card("C2", "22.2", "kei_agent_module:time:")

    assert await commands.post_cards(config, slack) == 1

    posted, = slack.posted()
    assert posted["channel"] == "C1" and posted["blocks"][1]["elements"][0]["action_id"] == START
    card = Records(Store(config.db_path), "time").get("card", "C1")
    assert card == {"channel": "C1", "ts": "1001.000", "buttons": "kei_agent_module:time:"}


def test_the_cards_command_needs_the_slack_token(capsys):
    assert commands.cards_main([]) == 1
    assert "SLACK_BOT_TOKEN" in capsys.readouterr().out
