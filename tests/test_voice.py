"""声のレイヤ（docs/voice.md）。

机の上で声で話す。**考えるのは OpenAI Realtime API**（`live.py`）で、依頼者のことは
道具として渡す（`tools.py`）。音の出し入れだけ手元でやる（`audio.py`）。
"""

from datetime import datetime

import pytest

from kei_agent_voice import events

# 出来事 → 言い方と顔（events.py）

def test_finished_work_is_announced_and_looks_up():
    """終わったことは、気づいてほしいので喋って顔を向ける。"""
    r = events.reaction({"kind": "done", "theme": "amr-query"})
    assert r.speaks and r.look and r.face == events.HAPPY
    assert "amr-query に頼んだ作業" in r.text and "Slack" in r.text


def test_receiving_a_request_changes_the_face_but_stays_quiet():
    """依頼を受けただけでは喋らない（依頼のたびに喋るとうるさい）。"""
    r = events.reaction({"kind": "working", "theme": "amr-query"})
    assert not r.speaks and not r.look


def test_the_morning_summary_is_held_not_spoken():
    """朝のまとめは手元に置くだけ（速い道で使う）。"""
    assert not events.reaction({"kind": "schedule", "items": []}).speaks


def test_a_deadline_is_read_in_words_not_in_the_slack_form():
    """声では「あと23時間で締切」ではなく、時刻で言う。"""
    r = events.reaction({"kind": "due", "title": "第3回レポート", "at": "2026-09-22T17:00"})
    assert r.text == "第3回レポートの締切、明日の17時までだよ。" and r.look


def test_the_usage_limit_says_when_it_comes_back():
    r = events.reaction({"kind": "limited", "reset_at": "2026-09-21T23:30"})
    assert "23時30分ごろ" in r.text and r.face == events.SLEEPY
    # 時刻が読めなくても、黙らない
    assert "しばらくしたら" in events.reaction({"kind": "limited"}).text


def test_waiting_for_an_answer_is_announced():
    """Slack を見ていないと、聞き返して止まっていることに気づけない。"""
    r = events.reaction({"kind": "awaiting", "theme": "amr-query"})
    assert r.speaks and r.look and r.face == events.DOUBT


def test_an_unknown_event_is_ignored():
    assert events.reaction({"kind": "とつぜんの何か"}) is None
    assert events.reaction({}) is None


# A2A の受け口（executor.py）

def test_the_event_can_come_in_the_body_or_the_metadata():
    from kei_agent_voice.executor import event_of

    assert event_of('{"kind": "done", "theme": "vlm"}', {}) == {"kind": "done", "theme": "vlm"}
    assert event_of("", {"skill": "notify", "kind": "failed"}) == {"kind": "failed"}
    # 壊れた JSON でも落ちない
    assert event_of("{こわれてる", {"kind": "done"}) == {"kind": "done"}


def test_the_conversation_is_cut_at_a_date_change_or_a_long_gap(tmp_path):
    """離席から戻ると話題も変わっている。前の文脈を引きずると的外れになる（docs/voice.md の6節）。"""
    import time

    from kei_agent_voice.think import GAP_SECONDS, Codex

    codex = Codex(tmp_path, tmp_path / "voice")
    now = time.time()
    codex.remember("01a0-thread", now)

    assert codex.session_id(now + 60) == "01a0-thread"          # 続きは続き
    assert codex.session_id(now + GAP_SECONDS + 1) is None      # 長く離席したら切る
    # 日付が変わったら切る（2時間経っていなくても）
    midnight = time.mktime(time.strptime(
        time.strftime("%Y-%m-%d", time.localtime(now + 86400)), "%Y-%m-%d"))
    assert codex.session_id(midnight + 60) is None



def test_codex_is_only_allowed_to_read(tmp_path):
    """実行するのは Slack の Kei Agent だけ。相談相手には読ませるだけ。"""
    from kei_agent_voice.think import Codex

    codex = Codex(tmp_path, tmp_path / "voice")

    resumed = codex.build_command("01a0", out=tmp_path / "o")
    assert resumed[:4] == ["codex", "exec", "resume", "01a0"]
    assert 'sandbox_mode="read-only"' in resumed
    assert "model_reasoning_effort=low" in resumed
    # `resume` は -s と -C を断る（`error: unexpected argument '-s' found` で即座に終わる）
    assert "-s" not in resumed and "-C" not in resumed

    fresh = codex.build_command(None, tmp_path / "o")
    assert "resume" not in fresh and fresh[fresh.index("-C") + 1] == str(tmp_path)


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


def _tools(config, codex=None):
    from kei_agent_voice.tools import Tools
    return Tools(_held(), config, codex=codex or _FakeCodex())


class _FakeCodex:
    def __init__(self, text="条件Bだけ落ちてるね。", failed=""):
        from kei_agent_voice.think import Turn
        self.turn = Turn(text=text, failed=failed)
        self.asked = []

    def ask(self, text, **kw):
        self.asked.append(text)
        return self.turn


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
    from kei_agent import ask as asks

    tools = _tools(config)
    spoken = tools.propose_request("10_amr-query", "学習曲線を描いて")

    assert "amr-query に、学習曲線を描いて、って頼むよ。いい？" in spoken
    assert asks.pending_asks(config) == []      # 下書きだけでは何も動かない

    assert "渡した" in tools.send_request()
    pending = asks.pending_asks(config)
    assert len(pending) == 1
    assert pending[0][1]["theme"] == "10_amr-query"

    # 2回続けて渡そうとしても、下書きは1回で消える
    assert "渡すものが無い" in tools.send_request()


def test_a_broken_tool_does_not_stop_the_conversation(config):
    """道具でつまずいても、例外ではなく喋れる文で返す（会話が止まる方が悪い）。"""
    tools = _tools(config)

    assert "持っていない" in tools.call("そんな道具", {})

    def broken(*a, **k):
        raise RuntimeError("こわれた")

    tools.get_status = broken
    assert "うまくいかなかった" in tools.call("get_status", {})


def test_the_research_question_goes_to_codex(config):
    codex = _FakeCodex()
    tools = _tools(config, codex)

    assert tools.call("ask_research", {"question": "amr-query は何を確かめていたか"}) \
        == "条件Bだけ落ちてるね。"
    assert codex.asked == ["amr-query は何を確かめていたか"]

    assert "調べられなかった" in _tools(config, _FakeCodex(failed="上限に当たった")).ask_research("ねえ")


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
    from kei_agent_voice.live import DEFAULT_MODEL, DEFAULT_VOICE, KEY_ENV, MODEL_ENV, Live

    plain = Live(tools=None, env={})
    assert plain.key == "" and plain.model == DEFAULT_MODEL and plain.voice == DEFAULT_VOICE

    swapped = Live(tools=None, env={KEY_ENV: "sk-test", MODEL_ENV: "gpt-realtime-2.1-mini"})
    assert swapped.key == "sk-test" and swapped.model == "gpt-realtime-2.1-mini"


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
        def call(self, name, arguments):
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

class _FakeBrain:
    def __init__(self):
        self.announced = []
        self.ran = 0

    async def run(self, on_said=None):
        import asyncio
        self.ran += 1
        await asyncio.Event().wait()

    def announce(self, text):
        self.announced.append(text)

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


async def test_nothing_is_said_while_the_microphone_is_closed(config):
    """既定では繋がない。知らせは顔だけになる（喋る口が無い）。"""
    brain, face = _FakeBrain(), _FakeFace()
    s = _session_for(config, brain, face)

    s.announce("終わったよ。", events.HAPPY)

    assert s.listening is False and brain.announced == []
    assert face.shown == [events.HAPPY]


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


async def test_what_was_said_is_written_down(config):
    from kei_agent_voice import journal

    s = _session_for(config)
    s._write("依頼者", "今日はよく寝られなかった")
    s._write("Kei", "そうか、それはしんどいね。")

    written = list((config.overview_dir / journal.VOICE_DIR).glob("*.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "今日はよく寝られなかった" in body and "しんどいね" in body
