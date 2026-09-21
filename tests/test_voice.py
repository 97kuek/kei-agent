"""声のレイヤ（docs/voice.md）。

机の上で喋る口。本体から出来事を受け取り、言い方と顔は自分で決める。
"""

import pytest

from kei_agent_voice import events, markers


def test_parse_pulls_out_the_markers():
    reply = markers.parse(
        "その線でいいと思う。\n"
        "📌 決定: 4条件で比べる\n"
        "🛠 依頼: 条件ごとの精度を集計して\n"
        "🎯 テーマ: #amr-query\n"
    )
    assert reply.decisions == ["4条件で比べる"]
    assert reply.requests == ["条件ごとの精度を集計して"]
    assert reply.theme == "amr-query"
    assert reply.spoken == "その線でいいと思う。"   # 目印の行は読み上げない


def test_sentences_split_and_drop_symbols():
    said = markers.sentences("`msclap` を入れたよ。**次は**照合だね\n- 図は outputs/ にある")
    assert said == ["msclap を入れたよ。", "次は照合だね", "図は outputs/ にある"]


def test_long_sentence_is_cut_at_a_comma():
    long = "、".join(["とても長い話がここに続く"] * 12) + "。"
    said = markers.sentences(long)
    assert len(said) > 1 and all(len(s) <= markers.MAX_SENTENCE for s in said)


# 会話


def test_engine_comes_from_env_so_the_voice_can_be_swapped():
    """AivisSpeech のような VOICEVOX 互換のエンジンに、コードを変えずに向け替えられる。"""
    from kei_agent_voice.speech import (
        DEFAULT_PORT,
        DEFAULT_SPEAKER,
        PORT_ENV,
        SPEAKER_ENV,
        SPEED_ENV,
        Voicevox,
    )

    plain = Voicevox.from_env(env={})
    assert plain.base == f"http://127.0.0.1:{DEFAULT_PORT}" and plain.speaker == DEFAULT_SPEAKER

    # 名前そのものは、下の test_the_real_environment_variables_are_the_ones_read で確かめる
    swapped = Voicevox.from_env(env={
        PORT_ENV: "10101",
        # AivisSpeech の話者 ID は 0 からの連番ではない
        SPEAKER_ENV: "888753760",
        SPEED_ENV: "1.0",
    })
    assert swapped.base == "http://127.0.0.1:10101"
    assert swapped.speaker == 888753760 and swapped.speed == 1.0

    # 読めない値で落ちない（起動しなくなるより、いまの声のままのほうがまし）
    broken = Voicevox.from_env(env={PORT_ENV: "みみっつ"})
    assert broken.base == f"http://127.0.0.1:{DEFAULT_PORT}"


def test_tuning_only_touches_what_the_engine_returned():
    """エンジンが持っていない項目は足さない（替えたエンジンで断られないように）。"""
    from kei_agent_voice.speech import TUNING, Voicevox

    engine = Voicevox()
    sent: dict = {}

    def fake_post(path, body=None, raw=False):
        if path.startswith("/audio_query"):
            # 古いエンジンや別のエンジンは pauseLengthScale を持たないことがある
            return {"speedScale": 1.0, "intonationScale": 1.0, "prePhonemeLength": 0.1}
        sent.update(body or {})
        return b"wav"

    engine._post = fake_post
    assert engine.synthesize("ためし") == b"wav"

    assert sent["speedScale"] == engine.speed
    assert sent["intonationScale"] == TUNING["intonationScale"]
    assert sent["prePhonemeLength"] == TUNING["prePhonemeLength"]
    # 返ってこなかった項目は、勝手に足さない
    assert "pauseLengthScale" not in sent and "postPhonemeLength" not in sent


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


def test_missed_notices_are_merged_into_one():
    """離席中の知らせは1回にまとめる（1件ずつ喋ると、離席が長いほど喋り続ける）。"""
    got = events.summary([{"kind": "done"}, {"kind": "done"}, {"kind": "failed"}])
    assert got == "離れている間に、2件終わった、1件うまくいかなかったよ。詳しくは Slack を見てね。"
    assert events.summary([{"kind": "working"}]) == ""


# 出し先の切り替え（mouth.py）

class FakeEngine:
    base = "http://127.0.0.1:10101"

    def __init__(self, broken=False):
        self.broken = broken
        self.said = []

    def synthesize(self, sentence):
        if self.broken:
            raise OSError("engine is down")
        self.said.append(sentence)
        return b"RIFF-wav"


def test_the_mac_speaker_is_used_when_there_is_no_stackchan(tmp_path):
    """Stack-chan を買う前でも作れる。住所を書かなければ Mac のスピーカー。"""
    from kei_agent_voice.mouth import Mouth

    played = []
    mouth = Mouth(FakeEngine(), url="", play_command=("true",))
    mouth._play = lambda wav: played.append(wav)

    mouth.say("聞いてるよ。")

    assert played == [b"RIFF-wav"] and mouth.engine.said == ["聞いてるよ。"]
    # 住所が無いので、生きているかを見に行きもしない
    assert mouth.stackchan_ready() is False


def test_it_falls_back_to_the_mac_when_stackchan_does_not_answer():
    """電源が入っていない日でも黙らない。"""
    from kei_agent_voice.mouth import Mouth

    played = []
    mouth = Mouth(FakeEngine(), url="http://127.0.0.1:9/")   # 誰もいない口
    mouth._play = lambda wav: played.append(wav)

    mouth.say("終わったよ。")

    assert played == [b"RIFF-wav"]


def test_a_dead_engine_does_not_raise():
    """合成できないときは黙る（途中で止まるより、静かなほうがまし）。"""
    from kei_agent_voice.mouth import Mouth

    mouth = Mouth(FakeEngine(broken=True), url="")
    mouth._play = lambda wav: pytest.fail("鳴らしてはいけない")

    mouth.say("聞いてるよ。")   # 例外にならないこと


# A2A の受け口（executor.py）

def test_the_event_can_come_in_the_body_or_the_metadata():
    from kei_agent_voice.executor import event_of

    assert event_of('{"kind": "done", "theme": "vlm"}', {}) == {"kind": "done", "theme": "vlm"}
    assert event_of("", {"skill": "notify", "kind": "failed"}) == {"kind": "failed"}
    # 壊れた JSON でも落ちない
    assert event_of("{こわれてる", {"kind": "done"}) == {"kind": "done"}


# 呼びかけの判定（wake.py）。実測で出た文字列をそのまま使う

def test_the_shapes_that_actually_came_out_of_the_microphone():
    """2026-09-21 にマイクで測って、実際に出た文字。"""
    from kei_agent_voice import wake

    # 当たってほしいもの
    assert wake.called("けい今日の予定は")          # 素直に出た回
    assert wake.called("経えーじぇんと今日の予定は")  # 漢字に化けた回
    assert wake.called("おいけい今日の予定は")        # 長音が落ちた回
    assert wake.called("ケイ、今日の予定は")          # カタカナ（実測では出なかったが、来ても拾う）

    # 当たってはいけないもの
    assert not wake.called("今日の予定は")            # 呼びかけが消えた回
    assert not wake.called("け今日の予定は")          # 「け」1文字だけでは拾わない
    assert not wake.called("今朝は寒いね")
    assert not wake.called("")


def test_the_name_is_dropped_from_the_request():
    """依頼として渡すのは、呼びかけを落とした残り。"""
    from kei_agent_voice import wake

    assert wake.request("けい、今日の予定は") == "今日の予定は"
    assert wake.request("経えーじぇんと、学習曲線を描いて") == "えーじぇんと、学習曲線を描いて"
    assert wake.request("おいけい 学習曲線を描いて") == "学習曲線を描いて"
    # 呼ばれていない文は、そのまま返す
    assert wake.request("今日の予定は") == "今日の予定は"


def test_a_name_late_in_the_sentence_is_not_a_call():
    """呼びかけは文の頭にある。後ろに出た同音の漢字で誤爆しない。"""
    from kei_agent_voice import wake

    assert not wake.called("この論文の経過をまとめて")
    assert not wake.called("時間を計測しておいて")


# 耳（ears.py）。Swift 側はマイクが要るので、標準出力の読み方だけを押さえる

def test_the_lines_from_the_listener_are_read_as_events():
    from kei_agent_voice.ears import _event

    assert _event('{"kind":"final","text":"けい今日の予定は"}') == {"kind": "final", "text": "けい今日の予定は"}
    assert _event('{"kind":"ready"}') == {"kind": "ready"}
    # コンパイルの警告などが混ざっても落ちない
    assert _event("warning: something") == {}
    assert _event("{こわれてる") == {} and _event("") == {}


def test_missing_swift_is_explained(tmp_path):
    """Xcode が無い機械で、黙って動かないのを避ける。"""
    from kei_agent_voice.ears import Ears, EarsUnavailable

    with pytest.raises(EarsUnavailable, match="見つかりません"), Ears(swift="swift-that-does-not-exist"):
        pass


def test_a_missing_listener_is_explained(tmp_path):
    from kei_agent_voice.ears import Ears, EarsUnavailable

    with pytest.raises(EarsUnavailable, match="聞く側のプログラムがありません"), \
            Ears(listener=tmp_path / "nope.swift"):
        pass


def test_errors_from_the_listener_are_raised():
    from kei_agent_voice.ears import Ears, EarsUnavailable

    ears = Ears()

    class FakeProc:
        stdout = iter(['{"kind":"ready"}\n', '{"kind":"error","text":"マイクが使えません"}\n'])

    ears.proc = FakeProc()
    with pytest.raises(EarsUnavailable, match="マイクが使えません"):
        list(ears.heard())


def test_the_listener_is_compiled_once_and_reused(tmp_path):
    """`swift <file>` は毎回コンパイルして 12 秒かかる。常駐なので、先に作って使い回す。"""
    from kei_agent_voice.ears import Ears

    source = tmp_path / "listen.swift"
    source.write_text("print(1)")
    cache = tmp_path / "cache"
    built = []

    class Fake(Ears):
        def _compile(self, compiler, out):
            built.append(out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("binary")

    ears = Fake(listener=source, cache=cache)
    first = ears.build()
    assert first.exists() and len(built) == 1

    ears.build()
    assert len(built) == 1          # 2度目は作り直さない

    source.write_text("print(2)")   # 元が新しくなったら作り直す
    import os
    os.utime(source, (first.stat().st_mtime + 10, first.stat().st_mtime + 10))
    ears.build()
    assert len(built) == 2


# 速い道（fast.py）。モデルを通さずに、手元のもので答える

@pytest.fixture
def held():
    return {"schedule": {"items": [
        {"at": "10:40", "end": "12:20", "icon": "🎓", "text": "データベース"},
        {"at": "13:00", "end": "14:00", "icon": "💼", "text": "ゆうちょ様AML"},
        {"at": "15:05", "end": "16:45", "icon": "🎓", "text": "情報通信ネットワークB"},
        {"at": "17:00", "end": "", "icon": "⏰", "text": "締切: 第3回レポート"},
    ]}}


def test_the_next_thing_is_answered_from_what_was_pushed(held):
    from datetime import datetime

    from kei_agent_voice import fast

    now = datetime(2026, 9, 21, 12, 30)
    assert fast.answer("次は", held, now) == "次は13時から ゆうちょ様AML だよ。".replace(" ", "")


def test_classes_and_deadlines_are_asked_separately(held):
    from datetime import datetime

    from kei_agent_voice import fast

    now = datetime(2026, 9, 21, 9, 0)
    assert "データベース" in fast.answer("今日の授業は", held, now)
    assert "情報通信ネットワークB" in fast.answer("今日の授業は", held, now)
    assert "ゆうちょ" not in fast.answer("今日の授業は", held, now)     # 会議は混ぜない
    assert "第3回レポート" in fast.answer("締切は", held, now)


def test_things_already_past_are_not_read_out(held):
    from datetime import datetime

    from kei_agent_voice import fast

    now = datetime(2026, 9, 21, 16, 0)
    got = fast.answer("今日の予定は", held, now)
    assert "データベース" not in got and "第3回レポート" in got
    assert fast.answer("今日の授業は", held, datetime(2026, 9, 21, 20, 0)) == "今日の授業はもう無いよ。"


def test_the_time_is_read_in_words(held):
    from datetime import datetime

    from kei_agent_voice import fast

    assert fast.answer("いま何時", held, datetime(2026, 9, 21, 14, 5)) == "いま14時5分だよ。"
    assert fast.answer("いま何時", held, datetime(2026, 9, 21, 14, 0)) == "いま14時だよ。"


def test_the_状況_comes_from_the_events_that_were_pushed():
    from kei_agent_voice import fast

    assert fast.answer("どんな感じ", {"running": 1, "done": 2}) == "今日は1件動いてる、2件終わったよ。"
    assert fast.answer("どんな感じ", {}) == "いまは何も動いてないよ。"
    assert "上限" in fast.answer("どんな感じ", {"limited": True})


def test_anything_else_goes_to_the_slow_road(held):
    """当てが外れても壊れない。Codex が答える。"""
    from kei_agent_voice import fast

    assert fast.answer("この論文どう思う", held) is None
    assert fast.answer("", held) is None


# 聞いて答える（session.py）

class FakeMouth:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)

    def face(self, expression):
        pass


async def test_only_what_is_addressed_to_kei_is_answered(held):
    from kei_agent_voice.session import VoiceSession

    mouth = FakeMouth()
    s = VoiceSession(held, mouth=mouth)

    await s.handle("今日の天気はどう")      # 呼びかけなし
    assert mouth.said == []

    from datetime import datetime
    await s.handle("けい、今日の授業は", now=datetime(2026, 9, 21, 9, 0))
    assert any("データベース" in t for t in mouth.said)


async def test_the_name_alone_gets_a_nod(held):
    from kei_agent_voice.session import NODDED, VoiceSession

    mouth = FakeMouth()
    await VoiceSession(held, mouth=mouth).handle("ケイ")
    assert mouth.said == [NODDED]


async def test_what_the_fast_road_cannot_answer_says_so_for_now(held):
    """遅い道（Codex）はまだ繋いでいない。黙らずに、その旨を言う。"""
    from kei_agent_voice.session import NODDED, NOT_YET, VoiceSession

    mouth = FakeMouth()
    await VoiceSession(held, mouth=mouth).handle("けい、この論文どう思う")
    assert mouth.said == [NODDED, NOT_YET]


async def test_a_broken_answer_does_not_stop_the_loop(held, monkeypatch):
    """1件捌けなくても、聞き続ける。"""
    import asyncio

    from kei_agent_voice import session as mod
    from kei_agent_voice.session import VoiceSession

    mouth = FakeMouth()
    s = VoiceSession(held, mouth=mouth)
    monkeypatch.setattr(mod.fast, "answer", lambda *a, **k: 1 / 0)

    s.heard.put_nowait("けい、今日の予定は")
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0.05)
    task.cancel()
    # 落ちずに、次を待っている
    assert not task.done() or task.cancelled()


def test_the_real_environment_variables_are_the_ones_read(monkeypatch):
    """定数の名前がずれていないか。

    `env=` を渡す試験だけだと、定数名が古いままでも通ってしまう（実際にすり抜けて、
    VOICEVOX の声のまま動いていた）。本物の環境変数から読ませて確かめる。
    """
    from kei_agent_voice.speech import Voicevox

    monkeypatch.setenv("KEI_AGENT_TTS_PORT", "10101")
    monkeypatch.setenv("KEI_AGENT_TTS_SPEAKER", "1937616896")

    engine = Voicevox.from_env()

    assert engine.base == "http://127.0.0.1:10101" and engine.speaker == 1937616896


# マイクの開け閉め（常に録らない）

async def test_the_microphone_is_closed_until_slack_turns_it_on(held):
    """既定では開けない。講義中などに録られないように。"""
    from kei_agent_voice.session import VoiceSession

    s = VoiceSession(held, mouth=FakeMouth())
    import asyncio
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0.05)
    assert s.listening is False
    task.cancel()


def test_turning_it_off_closes_the_listener_not_just_ignores_it(held):
    """閉じるときは、聞く側のプロセスごと終わらせる。"""
    from kei_agent_voice.session import VoiceSession

    closed = []

    class FakeEars:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)

        def heard(self):
            import time
            while True:
                time.sleep(0.01)

    s = VoiceSession(held, mouth=FakeMouth(), ears=FakeEars())
    import asyncio
    s._loop = asyncio.new_event_loop()
    s.set_listening(True)
    assert s.listening is True

    s.set_listening(False)
    assert closed == [True]


def test_the_listen_event_reaches_the_session():
    """本体が押した listen が、マイクの開け閉めになる。"""
    from kei_agent_voice import events
    from kei_agent_voice.executor import VoiceExecutor

    class FakeSession:
        def __init__(self):
            self.calls = []

        def set_listening(self, on):
            self.calls.append(on)

    ex = VoiceExecutor(mouth=FakeMouth())
    ex.session = FakeSession()
    ex._hold({"kind": "listen", "on": True})
    ex._hold({"kind": "listen", "on": False})

    assert ex.session.calls == [True, False]
    # listen では喋らない
    assert not events.reaction({"kind": "listen", "on": True}).speaks
