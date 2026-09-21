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
    from kei_agent_voice.speech import DEFAULT_PORT, DEFAULT_SPEAKER, Voicevox

    plain = Voicevox.from_env(env={})
    assert plain.base == f"http://127.0.0.1:{DEFAULT_PORT}" and plain.speaker == DEFAULT_SPEAKER

    swapped = Voicevox.from_env(env={
        "KEI_AGENT_VOICE_PORT": "10101",
        # AivisSpeech の話者 ID は 0 からの連番ではない
        "KEI_AGENT_VOICE_SPEAKER": "888753760",
        "KEI_AGENT_VOICE_SPEED": "1.0",
    })
    assert swapped.base == "http://127.0.0.1:10101"
    assert swapped.speaker == 888753760 and swapped.speed == 1.0

    # 読めない値で落ちない（起動しなくなるより、いまの声のままのほうがまし）
    broken = Voicevox.from_env(env={"KEI_AGENT_VOICE_PORT": "みみっつ"})
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
