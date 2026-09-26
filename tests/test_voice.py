"""声のレイヤ（docs/architecture.md の「声のレイヤ」）。

机の上で声で話す。**考えるのは OpenAI Realtime API**（`live.py`）で、依頼者のことは
道具として渡す（`tools.py`）。音の出し入れだけ手元でやる（`audio.py`）。
"""

import asyncio
import base64
import json
from datetime import datetime
from pathlib import Path

import pytest
from fakes import pending_asks

from kei_agent_voice import events

# 出来事 → 言い方と顔（events.py）

def test_finished_work_is_announced():
    """終わったことは、気づいてほしいので喋る。"""
    r = events.reaction({"kind": "done", "theme": "amr-query"})
    assert r.speaks and r.face == events.HAPPY
    assert "amr-query に頼んだ作業" in r.text and "Slack" in r.text


def test_receiving_a_request_changes_the_face_but_stays_quiet():
    """依頼を受けただけでは喋らない（依頼のたびに喋るとうるさい）。"""
    r = events.reaction({"kind": "working", "theme": "amr-query"})
    assert not r.speaks


def test_the_morning_summary_is_held_not_spoken():
    """朝のまとめは手元に置くだけ（速い道で使う）。"""
    assert not events.reaction({"kind": "schedule", "items": []}).speaks


def test_a_deadline_is_read_in_words_not_in_the_slack_form():
    """声では「あと23時間で締切」ではなく、時刻で言う。"""
    r = events.reaction({"kind": "due", "title": "第3回レポート", "at": "2026-09-22T17:00"})
    assert r.text == "第3回レポートの締切、明日の17時までだよ。"


def test_the_usage_limit_says_when_it_comes_back():
    r = events.reaction({"kind": "limited", "reset_at": "2026-09-21T23:30"})
    assert "23時30分ごろ" in r.text and r.face == events.SLEEPY
    # 時刻が読めなくても、黙らない
    assert "しばらくしたら" in events.reaction({"kind": "limited"}).text


def test_waiting_for_an_answer_is_announced():
    """Slack を見ていないと、聞き返して止まっていることに気づけない。"""
    r = events.reaction({"kind": "awaiting", "theme": "amr-query"})
    assert r.speaks and r.face == events.DOUBT


def test_an_unknown_event_is_ignored():
    assert events.reaction({"kind": "とつぜんの何か"}) is None
    assert events.reaction({}) is None


async def test_voice_restores_listening_setting_on_start(config, store, monkeypatch):
    from kei_agent import settings
    from kei_agent_voice import app
    from kei_agent_voice.executor import VoiceExecutor

    settings.set_listening(store, True)
    seen = []

    class FakeSession:
        async def run(self, listening=False):
            seen.append(listening)
            await asyncio.Event().wait()

        def set_listening(self, on):
            pass

    monkeypatch.setattr(app, "VoiceSession", lambda held, config=None, store=None: FakeSession())
    async with app._ears(VoiceExecutor(), config=config, store=store)(None):
        await asyncio.sleep(0)
    assert seen == [True]


# A2A の受け口（executor.py）

def test_the_event_can_come_in_the_body_or_the_metadata():
    from kei_agent_voice.executor import event_of

    assert event_of('{"kind": "done", "theme": "vlm"}', {}) == {"kind": "done", "theme": "vlm"}
    assert event_of("", {"skill": "notify", "kind": "failed"}) == {"kind": "failed"}
    # 壊れた JSON でも落ちない
    assert event_of("{こわれてる", {"kind": "done"}) == {"kind": "done"}


# 音の出し入れ（audio.py）。GPT-Live は音をそのままやりとりする

def test_the_length_of_a_chunk_is_counted_in_milliseconds():
    from kei_agent_voice.audio import CHUNK_BYTES, CHUNK_MS, ms_of

    assert ms_of(b"\x00" * CHUNK_BYTES) == CHUNK_MS
    assert ms_of(b"") == 0


def test_the_microphone_can_be_pointed_at_another_device():
    from kei_agent_voice.audio import DEFAULT_MIC, MIC_ENV, Microphone

    assert Microphone(env={}).device == DEFAULT_MIC
    assert Microphone(env={MIC_ENV: ":2"}).device == ":2"


def test_a_missing_ffmpeg_is_reported_not_swallowed(monkeypatch):
    """`ffmpeg` が無い機械でも、理由が分かる形で止まる。"""
    from kei_agent_voice import audio

    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(audio.subprocess, "Popen", missing)
    with pytest.raises(audio.Unavailable):
        audio.Microphone(env={}).__enter__()


def test_stopping_reports_how_much_was_actually_heard(monkeypatch):
    """割り込まれたとき、モデルに「ここまでしか聞かれていない」と伝えるのに使う。"""

    from kei_agent_voice import audio

    class FakeProc:
        def __init__(self, *a, **k):
            self.stdin = self
            self.killed = False

        def poll(self):
            return None

        def write(self, pcm):
            pass

        def flush(self):
            pass

        def close(self):
            pass

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            pass

    monkeypatch.setattr(audio.subprocess, "Popen", FakeProc)
    speaker = audio.Speaker()
    speaker.write(b"\x00" * (audio.RATE * audio.WIDTH))      # 1秒ぶん書く

    # 書いた長さより、経った時間の方が短いので、鳴ったのは経った時間ぶんだけ
    assert speaker.played_ms < 1000
    assert speaker.speaking is True

    # 書いた長さを越えて時間が経ったら、鳴り終わっている
    speaker._started -= 2
    assert speaker.played_ms == 1000 and speaker.speaking is False
    assert speaker.stop() == 1000


# 道具（tools.py）。依頼者のことは、こちらから渡すしかない

def _held():
    """本体が押してきたもの。**日付つき**（今日ぶんだけ渡していたのが前の欠陥）。"""
    return {"schedule": {"items": [
        {"date": "2026-09-21", "at": "16:45", "icon": "🎓", "text": "情報通信ネットワークB"},
        {"date": "2026-09-21", "at": "10:40", "icon": "🎓", "text": "データベース"},
        {"date": "2026-09-22", "at": "13:00", "icon": "💼", "text": "定例（会議室A）"},
        {"date": "2026-09-24", "at": "17:00", "icon": "⏰", "text": "締切: 第3回レポート"},
    ]}}


def _tools(config, handoff=None):
    from kei_agent_voice.tools import Tools
    return Tools(_held(), config, handoff=handoff or _FakeHandoff())


class _FakeHandoff:
    def __init__(self, text="条件Bだけ落ちてるね。"):
        self.text = text
        self.asked = []

    async def ask(self, actor, question, theme=""):
        self.asked.append((actor, question, theme))
        return self.text


def test_tomorrow_is_not_answered_with_today(config):
    """実測の欠陥。「明日の予定」「今週の予定」に、どちらも今日の予定を答えていた。"""
    tools = _tools(config)
    now = datetime(2026, 9, 21, 15, 0)

    today = tools.get_schedule("today", "all", now)
    assert "情報通信ネットワークB" in today
    assert "データベース" not in today          # もう終わっている時刻のものは出さない
    assert "定例" not in today

    tomorrow = tools.get_schedule("tomorrow", "all", now)
    assert "定例" in tomorrow and "情報通信ネットワークB" not in tomorrow

    week = tools.get_schedule("week", "all", now)
    assert "定例" in week and "第3回レポート" in week


def test_the_kind_can_be_narrowed(config):
    tools = _tools(config)
    now = datetime(2026, 9, 21, 9, 0)

    assert "データベース" in tools.get_schedule("today", "class", now)
    assert "入っていない" in tools.get_schedule("today", "meeting", now)
    assert "第3回レポート" in tools.get_schedule("week", "due", now)


def test_the_status_comes_from_the_events_that_were_pushed(config):
    from kei_agent_voice.tools import Tools

    assert "1件動いている" in Tools({"running": 1}, config).get_status()
    assert Tools({}, config).get_status() == "いまは何も動いていない。"
    assert "上限" in Tools({"limited": True}, config).get_status()


def test_a_job_is_not_handed_over_until_it_was_confirmed(config):
    """1回の思い違いで作業が動き出さないように、下書きと渡すのを分ける。"""

    tools = _tools(config)
    spoken = tools.propose_request("10_amr-query", "学習曲線を描いて")

    assert "amr-query に、学習曲線を描いて、って頼むよ。いい？" in spoken
    assert pending_asks(config) == []      # 下書きだけでは何も動かない

    assert "渡した" in tools.send_request()
    pending = pending_asks(config)
    assert len(pending) == 1
    assert pending[0][1]["theme"] == "10_amr-query"

    # 2回続けて渡そうとしても、下書きは1回で消える
    assert "渡すものが無い" in tools.send_request()


def test_claiming_asks_recovers_an_interrupted_processing_file(config):
    """再起動後、前回 claim 済みの依頼をもう一度処理できる。"""
    from kei_agent import ask as asks

    path = asks.write_ask(config, "amr-query", "学習曲線を描いて")
    processing = path.with_name(f"{path.name}.processing")
    path.rename(processing)

    asks.recover_asks(config)
    claimed = asks.claim_asks(config)

    assert len(claimed) == 1
    assert claimed[0].path == processing
    assert claimed[0].payload["text"] == "学習曲線を描いて"
    assert processing.exists() and not path.exists()


def test_claiming_asks_does_not_reclaim_an_active_processing_file(config):
    """通常の poll は、別の処理が所有している依頼を奪わない。"""
    from kei_agent import ask as asks

    asks.write_ask(config, "amr-query", "学習曲線を描いて")
    claimed = asks.claim_asks(config)

    assert len(claimed) == 1
    assert asks.claim_asks(config) == []
    assert claimed[0].path.exists()


def test_writing_an_ask_fsyncs_a_temporary_file_before_exposing_json(config, monkeypatch):
    """consumer には、完全に書けた JSON だけを原子的に公開する。"""
    from kei_agent import ask as asks

    real_fsync = asks.os.fsync
    real_replace = asks.os.replace
    events = []

    def fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def replace(source, target):
        source = Path(source)
        target = Path(target)
        assert events == ["fsync"]
        assert source.parent == target.parent
        assert source.suffix != ".json"
        assert not target.exists()
        assert json.loads(source.read_text(encoding="utf-8"))["text"] == "学習曲線を描いて"
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(asks.os, "fsync", fsync)
    monkeypatch.setattr(asks.os, "replace", replace)

    path = asks.write_ask(config, "amr-query", "学習曲線を描いて")

    assert events == ["fsync", "replace"]
    assert json.loads(path.read_text(encoding="utf-8"))["text"] == "学習曲線を描いて"


def test_claiming_asks_discards_a_non_object_payload(config):
    """依頼の JSON がオブジェクトでなければ、処理せずに破棄する。"""
    from kei_agent import ask as asks

    path = asks.write_ask(config, "amr-query", "学習曲線を描いて")
    path.write_text("[]", encoding="utf-8")

    assert asks.claim_asks(config) == []
    assert not path.exists()


async def test_a_broken_tool_does_not_stop_the_conversation(config):
    """道具でつまずいても、例外ではなく喋れる文で返す（会話が止まる方が悪い）。"""
    tools = _tools(config)

    assert "持っていない" in await tools.call("そんな道具", {})

    def broken(*a, **k):
        raise RuntimeError("こわれた")

    tools.get_status = broken
    assert "うまくいかなかった" in await tools.call("get_status", {})


async def test_the_research_question_goes_to_the_selected_agent(config):
    handoff = _FakeHandoff()
    tools = _tools(config, handoff)

    assert await tools.call("ask_agent", {"agent": "research", "theme": "amr-query",
                                     "question": "amr-query は何を確かめていたか"}) \
        == "条件Bだけ落ちてるね。"
    assert handoff.asked == [("research", "amr-query は何を確かめていたか", "amr-query")]

    assert await _tools(config, _FakeHandoff("上限に当たった")).ask_agent("research", "ねえ") == "上限に当たった"


# Realtime API とのやりとり（live.py）

def test_the_session_is_set_up_the_way_the_api_wants_it():
    """調べて分かった形をそのまま押さえる（間違えると黙って英語で喋り出す）。"""
    from kei_agent_voice import audio
    from kei_agent_voice.live import _session
    from kei_agent_voice.tools import DEFINITIONS

    sent = _session("cedar", DEFINITIONS)["session"]

    assert sent["type"] == "realtime"
    # `modalities` はもう無い。そして text と audio は同時に頼めない
    assert sent["output_modalities"] == ["audio"]
    assert "modalities" not in sent
    # 入れないとサーバー既定の英語の人格になる
    assert "日本語" in sent["instructions"]
    # PCM は 24000Hz のみ
    assert sent["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": audio.RATE}
    assert sent["audio"]["output"]["voice"] == "cedar"
    # 喋り出したら、こちらの返事はサーバーが止めてくれる
    assert sent["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert [t["name"] for t in sent["tools"]] == [d["name"] for d in DEFINITIONS]


def test_the_key_is_read_from_the_environment():
    from kei_agent_voice.live import DEFAULT_MODEL, DEFAULT_VOICE, KEY_ENV, Live

    plain = Live(tools=None, env={})
    assert plain.key == "" and plain.model == DEFAULT_MODEL and plain.voice == DEFAULT_VOICE

    fixed = Live(tools=None, env={KEY_ENV: "sk-test", "KEI_AGENT_REALTIME_MODEL": "other"})
    assert fixed.key == "sk-test" and fixed.model == DEFAULT_MODEL
    with pytest.raises(TypeError):
        Live(tools=None, model="other")


async def test_without_a_key_it_says_so_instead_of_hanging():
    from kei_agent_voice.live import KEY_ENV, Live, Unavailable

    with pytest.raises(Unavailable) as found:
        await Live(tools=None, env={}).run()
    assert KEY_ENV in str(found.value)


async def test_being_interrupted_tells_the_model_how_much_was_heard():
    """伝えないと、モデルは全部聞かれたつもりで話を続ける。超えるとサーバーが断る。"""
    from kei_agent_voice.live import TRUNCATE_MARGIN_MS, Live

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    brain = Live(tools=None, env={})
    brain.speaker.stop = lambda: 1500
    brain.speaking_item = "item_123"
    ws = FakeWs()

    await brain._interrupt(ws)

    assert ws.sent == [{"type": "conversation.item.truncate", "item_id": "item_123",
                        "content_index": 0, "audio_end_ms": 1500 - TRUNCATE_MARGIN_MS}]
    # 止めるのはサーバーがやるので、こちらから response.cancel は送らない
    assert not any(s["type"] == "response.cancel" for s in ws.sent)

    # まだ何も鳴っていなければ、何も言わない
    brain.speaker.stop = lambda: 0
    brain.speaking_item = "item_456"
    quiet = FakeWs()
    await brain._interrupt(quiet)
    assert quiet.sent == []


async def test_a_tool_call_is_answered_and_the_model_is_told_to_continue():
    """`response.create` を送らないと、モデルは黙ったまま。"""
    from kei_agent_voice.live import Live

    class FakeTools:
        async def call(self, name, arguments):
            return f"{name} の答え"

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    brain = Live(tools=FakeTools(), env={})
    ws = FakeWs()
    await brain._answer_tools(ws, {"response": {"output": [
        {"type": "function_call", "name": "get_status", "call_id": "call_1", "arguments": "{}"}]}})

    assert ws.sent[0]["item"]["type"] == "function_call_output"
    assert ws.sent[0]["item"]["call_id"] == "call_1"
    assert "get_status の答え" in ws.sent[0]["item"]["output"]
    assert ws.sent[-1] == {"type": "response.create"}

    # 道具を呼ばれていないときは、何も送らない
    quiet = FakeWs()
    await brain._answer_tools(quiet, {"response": {"output": []}})
    assert quiet.sent == []


# 繋ぎ目（session.py）

class _NoticeSocket:
    def __init__(self, events=()):
        self.incoming = asyncio.Queue()
        for event in events:
            self.incoming.put_nowait(event)
        self.sent = []
        self.closed = False
        self.receiving = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def send_json(self, event):
        self.sent.append(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        from types import SimpleNamespace

        import aiohttp

        self.receiving.set()
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))


class _NoticeSpeaker:
    def __init__(self):
        self.pcm = []
        self.stopped = False
        self.playback_done = asyncio.Event()
        self.playback_done.set()

    def begin_item(self):
        pass

    def write(self, pcm):
        self.pcm.append(pcm)

    def stop(self):
        self.stopped = True

    async def wait_until_done(self):
        await self.playback_done.wait()


def _notice_connection(monkeypatch, socket, speaker):
    from kei_agent_voice import live

    class Http:
        closed = False
        headers = None
        url = None

        def __init__(self, headers):
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True

        def ws_connect(self, url, heartbeat):
            self.url = url
            assert heartbeat == 20
            return socket

    http = Http({})

    def connect(headers):
        http.headers = headers
        return http

    monkeypatch.setattr(live.aiohttp, "ClientSession", connect)
    monkeypatch.setattr(live.audio, "Speaker", lambda: speaker)
    return http


@pytest.mark.parametrize("done", ["response.output_audio.done", "response.done"])
async def test_say_once_sends_one_notice_without_opening_microphone(monkeypatch, done):
    from kei_agent_voice.live import Live

    socket = _NoticeSocket([
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\0\0").decode()},
        {"type": "response.output_audio_transcript.done", "transcript": "終わったよ。"},
        {"type": done, "response": {"status": "completed", "output": []}},
    ])
    speaker = _NoticeSpeaker()
    http = _notice_connection(monkeypatch, socket, speaker)
    brain = Live(tools=None, key="test")

    async def forbidden(*args):
        pytest.fail("通知でマイクや道具を開始した")

    monkeypatch.setattr(brain, "_send_microphone", forbidden)
    monkeypatch.setattr(brain, "_answer_tools", forbidden)
    said = []
    await asyncio.wait_for(brain.say_once("終わったよ", lambda *args: said.append(args)), 1)

    assert http.headers == {"Authorization": "Bearer test"}
    assert http.url.endswith(f"?model={brain.model}")
    assert [event["type"] for event in socket.sent] == [
        "session.update", "conversation.item.create", "response.create"]
    assert socket.sent[0]["session"]["tools"] == []
    assert socket.sent[1]["item"]["content"][0]["text"] == "（お知らせ）終わったよ"
    assert speaker.pcm == [b"\0\0"]
    assert said == [("Kei", "終わったよ。")]
    assert speaker.stopped and socket.closed and http.closed and brain._ws is None


async def test_say_once_waits_for_playback_before_cleanup(monkeypatch):
    from kei_agent_voice.live import Live

    socket = _NoticeSocket([{"type": "response.output_audio.done"}])
    speaker = _NoticeSpeaker()
    speaker.playback_done.clear()
    http = _notice_connection(monkeypatch, socket, speaker)
    task = asyncio.create_task(Live(None, key="test").say_once("終わったよ"))
    try:
        await asyncio.wait_for(socket.receiving.wait(), 1)
        await asyncio.sleep(0)
        assert not task.done() and not speaker.stopped
        speaker.playback_done.set()
        await asyncio.wait_for(task, 1)
        assert speaker.stopped and http.closed and socket.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_say_once_cancellation_closes_all_resources(monkeypatch):
    from kei_agent_voice.live import Live

    socket, speaker = _NoticeSocket(), _NoticeSpeaker()
    http = _notice_connection(monkeypatch, socket, speaker)
    brain = Live(None, key="test")
    task = asyncio.create_task(brain.say_once("終わったよ"))
    await asyncio.wait_for(socket.receiving.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert speaker.stopped and http.closed and socket.closed and brain._ws is None


@pytest.mark.parametrize("event", [
    {"type": "error", "error": {"message": "denied"}},
    {"type": "response.done", "response": {"status": "failed"}},
    None,
])
async def test_say_once_failure_closes_all_resources(monkeypatch, event):
    from kei_agent_voice.live import Live, Unavailable

    socket, speaker = _NoticeSocket([event]), _NoticeSpeaker()
    http = _notice_connection(monkeypatch, socket, speaker)
    brain = Live(None, key="test")
    with pytest.raises(Unavailable):
        await asyncio.wait_for(brain.say_once("終わったよ"), 1)
    assert speaker.stopped and http.closed and socket.closed and brain._ws is None


async def test_say_once_requires_a_key():
    from kei_agent_voice.live import Live, Unavailable

    with pytest.raises(Unavailable, match="OPENAI_API_KEY"):
        await Live(None, env={}).say_once("終わったよ")

class _FakeBrain:
    def __init__(self):
        self.announced = []
        self.said_once = []
        self.notice_started = asyncio.Queue()
        self.notice_release = asyncio.Event()
        self.notice_release.set()
        self.notice_finished = asyncio.Queue()
        self.notice_cancelled = False
        self.failure = None
        self.on_said = None
        self.ran = 0

    async def run(self, on_said=None):
        import asyncio
        self.ran += 1
        self.on_said = on_said
        await asyncio.Event().wait()

    def announce(self, text):
        self.announced.append(text)
        if self.on_said:
            self.on_said("Kei", text)

    async def say_once(self, text, on_said=None):
        self.said_once.append(text)
        self.notice_started.put_nowait(text)
        try:
            await self.notice_release.wait()
        except asyncio.CancelledError:
            self.notice_cancelled = True
            raise
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        if on_said:
            on_said("Kei", text)
        self.notice_finished.put_nowait(text)

    def close(self):
        pass


class _FakeFace:
    def __init__(self):
        self.shown = []

    def show(self, expression):
        self.shown.append(expression)
        return True


def _session_for(config, brain=None, face=None):
    from kei_agent_voice.session import VoiceSession
    return VoiceSession({}, config=config, brain=brain or _FakeBrain(),
                        face=face or _FakeFace())


async def test_notice_is_spoken_with_short_session_when_not_listening(config):
    """非会話中の通知は受信順に鳴らし、呼び出し元を待たせない。"""
    brain, face = _FakeBrain(), _FakeFace()
    brain.notice_release.clear()
    s = _session_for(config, brain, face)
    task = asyncio.create_task(s.run())
    try:
        s.announce("一件目", events.HAPPY)
        s.announce("二件目", events.HAPPY)
        assert face.shown == [events.HAPPY, events.HAPPY]
        assert not s.listening and brain.ran == 0 and brain.announced == []
        assert await asyncio.wait_for(brain.notice_started.get(), 1) == "一件目"
        await asyncio.sleep(0)
        assert brain.said_once == ["一件目"]
        brain.notice_release.set()
        assert await asyncio.wait_for(brain.notice_finished.get(), 1) == "一件目"
        assert await asyncio.wait_for(brain.notice_finished.get(), 1) == "二件目"
        assert brain.said_once == ["一件目", "二件目"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert s._notice_worker.done()


async def test_notice_uses_existing_connection(config):
    brain = _FakeBrain()
    s = _session_for(config, brain)
    task = asyncio.create_task(s.run(listening=True))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        s.announce("終わったよ。")
        assert brain.announced == ["終わったよ。"] and brain.said_once == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_notice_worker_is_cancelled_and_awaited_on_shutdown(config):
    brain = _FakeBrain()
    brain.notice_release.clear()
    s = _session_for(config, brain)
    task = asyncio.create_task(s.run())
    try:
        s.announce("一件目")
        s.announce("二件目")
        await asyncio.wait_for(brain.notice_started.get(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert brain.notice_cancelled and s._notice_worker.done()
    assert brain.said_once == ["一件目"]


@pytest.mark.parametrize("unavailable", [True, False])
async def test_notice_worker_continues_after_failure(config, caplog, unavailable):
    from kei_agent_voice.live import Unavailable

    brain = _FakeBrain()
    brain.failure = Unavailable("繋がらない") if unavailable else RuntimeError("壊れた")
    s = _session_for(config, brain)
    task = asyncio.create_task(s.run())
    try:
        s.announce("一件目")
        s.announce("二件目")
        assert await asyncio.wait_for(brain.notice_finished.get(), 1) == "二件目"
        assert brain.said_once == ["一件目", "二件目"]
        assert "繋がらない" in caplog.text if unavailable else "壊れた" in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_turning_the_microphone_on_connects_and_off_disconnects(config):
    import asyncio

    brain = _FakeBrain()
    s = _session_for(config, brain)

    s.set_listening(True)
    await asyncio.sleep(0.01)
    assert s.listening is True and brain.ran == 1

    s.announce("終わったよ。", events.HAPPY)
    assert brain.announced == ["終わったよ。"]

    s.set_listening(False)
    assert s.listening is False


async def test_the_conversation_is_not_written_down(config):
    """声の会話は残さない（依頼は Slack のスレッドに残る）。控えを受け取る口も渡さない。"""
    brain = _FakeBrain()
    s = _session_for(config, brain)
    task = asyncio.create_task(s.run(listening=True))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert brain.ran == 1 and brain.on_said is None
        s.set_listening(False)
        s.announce("終わったよ。")
        await asyncio.wait_for(brain.notice_finished.get(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not (config.overview_dir / "voice").exists()


# 直した欠陥

class _SentWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def test_ask_agent_runs_on_the_loop_with_the_lifespan_store(config, store):
    """sqlite はつないだスレッドでしか使えない。別スレッドで asyncio.run すると必ず落ちていた。"""
    from kei_agent_voice.live import Live
    from kei_agent_voice.tools import Tools

    class StoreHandoff:
        async def ask(self, actor, question, theme=""):
            store.conn.execute("select 1")
            return "調べた答え"

    tools = Tools(_held(), config, handoff=StoreHandoff())
    ws = _SentWs()
    await Live(tools, env={})._answer_tools(ws, {"response": {"output": [
        {"type": "function_call", "name": "ask_agent", "call_id": "c1",
         "arguments": json.dumps({"agent": "work", "question": "今日の会議は？"})}]}})

    assert "調べた答え" in ws.sent[0]["item"]["output"]


async def test_voice_questions_go_only_to_the_orchestrator(config, monkeypatch):
    """担当を呼べるのは本体だけ。声のレイヤは本体の `ask` に JSON で頼み、答えの文だけを受け取る。"""
    from dataclasses import replace

    from kei_agent import agents
    from kei_agent_voice.handoff import Handoff
    from kei_agent_voice.tools import Tools

    config = replace(config, a2a=replace(config.a2a, orchestrator="http://127.0.0.1:8786"))
    asked = []

    async def ask(agent, skill, params=None, on_progress=None, text=""):
        asked.append((agent.base_url, skill, json.loads(text)))
        return agents.Reply(text="今日は2コマだよ")

    monkeypatch.setattr(agents, "ask", ask)
    tools = Tools({}, config)
    assert isinstance(tools.handoff, Handoff)
    assert await tools.ask_agent("course", "今日の授業は？") == "今日は2コマだよ"
    assert asked == [(config.a2a.orchestrator, "ask", {"actor": "course", "question": "今日の授業は？", "theme": ""})]


async def test_voice_says_so_when_the_orchestrator_is_not_configured(config):
    from dataclasses import replace

    from kei_agent_voice.handoff import NO_ORCHESTRATOR, Handoff

    local = replace(config, a2a=replace(config.a2a, orchestrator=""))
    assert await Handoff(local).ask("work", "今日の会議は？") == NO_ORCHESTRATOR


async def test_voice_session_starts_with_the_saved_listening_setting(config, store, monkeypatch):
    """マイクを開けるかは、本体が App Home から押して保存した設定で始める（既定は切）。"""
    from kei_agent import settings
    from kei_agent_voice import app
    from kei_agent_voice.executor import VoiceExecutor

    seen = []

    class FakeSession:
        def __init__(self, held, config=None):
            pass

        async def run(self, listening=False):
            seen.append(listening)
            await asyncio.Event().wait()

    monkeypatch.setattr(app, "VoiceSession", FakeSession)
    settings.set_listening(store, True)
    async with app._ears(VoiceExecutor(), config=config, store=store)(None):
        await asyncio.sleep(0)
    assert seen == [True]


async def test_answering_tools_does_not_block_receiving(monkeypatch):
    """ask_agent は数秒かかる。そのあいだも割り込みや音を受けられる。"""
    from kei_agent_voice.live import Live

    release = asyncio.Event()

    class SlowTools:
        async def call(self, name, arguments):
            await release.wait()
            return "遅い答え"

    brain = Live(SlowTools(), env={})
    brain.speaker = _NoticeSpeaker()
    socket = _NoticeSocket([
        {"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "ask_agent", "call_id": "c1", "arguments": "{}"}]}},
        {"type": "response.output_audio.delta", "item_id": "i1",
         "delta": base64.b64encode(b"\0\0").decode()},
        None,
    ])
    await asyncio.wait_for(brain._receive(socket, None), 1)
    assert brain.speaker.pcm == [b"\0\0"]
    assert len(brain._tool_tasks) == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(*brain._tool_tasks), 1)
    assert socket.sent[0]["item"]["call_id"] == "c1"


def _fake_speaker_proc(monkeypatch):
    from kei_agent_voice import audio

    class FakeProc:
        def __init__(self, *a, **k):
            self.stdin = self

        def poll(self):
            return None

        def write(self, pcm):
            pass

        def flush(self):
            pass

        def close(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            pass

    monkeypatch.setattr(audio.subprocess, "Popen", FakeProc)
    return audio


def test_a_new_item_is_measured_from_its_own_start(monkeypatch):
    """2つめの返事で割り込まれたとき、1つめの長さまで足して伝えていた。"""
    audio = _fake_speaker_proc(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(audio.time, "monotonic", lambda: clock[0])
    speaker = audio.Speaker()
    speaker.begin_item()
    speaker.write(b"\x00" * (audio.RATE * audio.WIDTH))      # 1秒ぶん
    clock[0] += 30                                            # 鳴り終わってしばらく経つ
    speaker.begin_item()
    speaker.write(b"\x00" * (audio.RATE * audio.WIDTH * 2))  # 2秒ぶん
    clock[0] += 0.5
    assert speaker.played_ms == 500

    # 前の返事がまだ鳴っていれば、次の返事はそれが終わってから鳴り始める
    speaker.begin_item()
    speaker.write(b"\x00" * (audio.RATE * audio.WIDTH))
    clock[0] += 1.0                                           # 前の返事の残りは1.5秒
    assert speaker.played_ms == 0
    clock[0] += 1.0
    assert speaker.played_ms == 500


async def test_the_receiver_starts_a_new_item_when_the_item_changes():
    from kei_agent_voice.live import Live

    class ItemSpeaker(_NoticeSpeaker):
        def __init__(self):
            super().__init__()
            self.items = 0

        def begin_item(self):
            self.items += 1

    brain = Live(None, env={})
    brain.speaker = ItemSpeaker()
    delta = base64.b64encode(b"\0\0").decode()
    socket = _NoticeSocket([
        {"type": "response.output_audio.delta", "item_id": "a", "delta": delta},
        {"type": "response.output_audio.delta", "item_id": "a", "delta": delta},
        {"type": "response.output_audio.delta", "item_id": "b", "delta": delta},
        None,
    ])
    await asyncio.wait_for(brain._receive(socket, None), 1)
    assert brain.speaker.items == 2 and brain.speaking_item == "b"


def test_audio_and_live_share_one_unavailable():
    from kei_agent_voice import audio, live

    assert live.Unavailable is audio.Unavailable


async def test_a_dead_microphone_ends_the_session(monkeypatch):
    """マイクが死んだら、黙って聞いているふりを続けない。"""
    from kei_agent_voice.live import Live, Unavailable

    socket, speaker = _NoticeSocket(), _NoticeSpeaker()
    _notice_connection(monkeypatch, socket, speaker)
    brain = Live(None, key="test")

    async def dead():
        raise Unavailable("マイクの許可がありません")

    monkeypatch.setattr(brain, "_send_microphone", dead)
    with pytest.raises(Unavailable, match="許可"):
        await asyncio.wait_for(brain._once(None), 1)
    assert socket.closed and brain._ws is None


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_is_not_retried(monkeypatch, status):
    import aiohttp

    from kei_agent_voice.live import Live, Unavailable

    brain = Live(None, key="test")
    tries = []

    async def once(on_said):
        tries.append(1)
        raise aiohttp.WSServerHandshakeError(None, (), status=status)

    monkeypatch.setattr(brain, "_once", once)
    with pytest.raises(Unavailable):
        await asyncio.wait_for(brain.run(), 1)
    assert tries == [1]


async def test_reconnecting_backs_off_on_every_exit(monkeypatch):
    import aiohttp

    from kei_agent_voice import live

    brain = live.Live(None, key="test")
    outcomes = [aiohttp.ClientError("切れた"), None, TimeoutError(), None, None, None, None, None]
    waits = []

    async def once(on_said):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def sleep(seconds):
        waits.append(seconds)
        if not outcomes:
            raise asyncio.CancelledError

    monkeypatch.setattr(brain, "_once", once)
    monkeypatch.setattr(live.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await brain.run()
    assert waits == sorted(waits) and waits[0] == live.BACKOFF_SECONDS
    assert waits[-1] == live.BACKOFF_MAX_SECONDS and len(waits) == 8


async def test_say_once_gives_up_after_a_while(monkeypatch):
    from kei_agent_voice import live

    socket, speaker = _NoticeSocket(), _NoticeSpeaker()
    _notice_connection(monkeypatch, socket, speaker)
    monkeypatch.setattr(live, "SAY_ONCE_SECONDS", 0.05)
    with pytest.raises(live.Unavailable):
        await asyncio.wait_for(live.Live(None, key="test").say_once("終わったよ"), 1)
    assert speaker.stopped and socket.closed


def test_the_week_uses_the_given_day_for_undated_items(config):
    from kei_agent_voice.tools import Tools

    held = {"schedule": {"items": [{"at": "09:00", "text": "朝の会"}]}}
    now = datetime(2026, 9, 21, 8, 0)
    assert "09月21日 09:00 朝の会" in Tools(held, config).get_schedule("week", "all", now)


def test_the_limit_is_forgotten_after_the_reset_time(config):
    from kei_agent_voice.executor import current
    from kei_agent_voice.tools import Tools

    held = {"limited": True, "limited_until": "2026-09-21T23:30"}
    assert "上限" in Tools(held, config).get_status(datetime(2026, 9, 21, 23, 0))
    assert "上限" not in Tools(held, config).get_status(datetime(2026, 9, 21, 23, 31))
    assert "limited" not in current(held, datetime(2026, 9, 21, 23, 31))


def test_the_counts_start_over_each_day(config):
    from kei_agent_voice.executor import VoiceExecutor
    from kei_agent_voice.tools import Tools

    executor = VoiceExecutor()
    executor._hold({"kind": "working"}, datetime(2026, 9, 21, 10, 0))
    executor._hold({"kind": "done"}, datetime(2026, 9, 21, 11, 0))
    executor._hold({"kind": "failed"}, datetime(2026, 9, 21, 12, 0))
    tools = Tools(executor.held, config)
    assert "1件終わった" in tools.get_status(datetime(2026, 9, 21, 13, 0))
    assert tools.get_status(datetime(2026, 9, 22, 9, 0)) == "いまは何も動いていない。"
    executor._hold({"kind": "done"}, datetime(2026, 9, 22, 10, 0))
    assert executor.held["done"] == 1 and "failed" not in executor.held


async def test_a_notice_waits_while_a_response_is_being_spoken():
    from kei_agent_voice.live import Live

    brain = Live(None, env={})
    brain.speaker = _NoticeSpeaker()
    socket = _NoticeSocket([{"type": "response.created"}])
    brain._ws = socket
    receiving = asyncio.create_task(brain._receive(socket, None))
    notices = asyncio.create_task(brain._send_notices())
    try:
        await asyncio.sleep(0.01)
        brain.announce("終わったよ")
        await asyncio.sleep(0.01)
        assert socket.sent == []
        socket.incoming.put_nowait({"type": "response.done", "response": {"output": []}})
        await asyncio.sleep(0.01)
        assert [e["type"] for e in socket.sent] == ["conversation.item.create", "response.create"]
    finally:
        socket.incoming.put_nowait(None)
        notices.cancel()
        await asyncio.gather(receiving, notices, return_exceptions=True)


def test_closing_drops_pending_notices():
    from kei_agent_voice.live import Live

    brain = Live(None, env={})
    brain.announce("一件目")
    brain.close()
    assert brain._notices.empty()
