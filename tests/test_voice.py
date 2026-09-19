"""机の上の音声対話（docs/plan.md の13章）。"""

import json
import time
from pathlib import Path

import pytest

from kei_agent import ask
from kei_agent_voice import bridge, journal, markers
from kei_agent_voice.app import Session
from kei_agent_voice.brain import Codex, today, wants_deep


class FakeSpeaker:
    """読み上げの代わり。喋った文と、止められた回数を覚える。"""

    def __init__(self):
        self.said: list[str] = []
        self.stopped = 0
        self.enabled = True

    def say(self, sentence):
        self.said.append(sentence)

    def stop(self):
        self.stopped += 1


class FakeCodex:
    """Codex の代わり。決めた返事を返し、渡された文を覚える。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.asked: list[str] = []
        self.forgotten = 0

    def ask(self, text, day, deep=None, on_text=None):
        self.asked.append(text)
        turn = self.replies.pop(0) if self.replies else _turn("うん")
        if on_text and turn.text:
            on_text(turn.text)
        return turn

    def forget(self):
        self.forgotten += 1


def _turn(text="", failed=None, limit_reset=None):
    from kei_agent_voice.brain import Turn
    return Turn(text=text, failed=failed, limit_reset=limit_reset)


@pytest.fixture
def session(config):
    speaker = FakeSpeaker()
    codex = FakeCodex()
    return Session(config=config, codex=codex, speaker=speaker), codex, speaker


# 目印と読み上げ

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

def test_reply_is_spoken_and_written_down(session, config):
    s, codex, speaker = session
    codex.replies = [_turn("そこは4条件で比べるのがいいと思う。\n🎯 テーマ: amr-query")]

    s.handle("条件の比べ方どうしよう")

    assert speaker.said == ["そこは4条件で比べるのがいいと思う。"]
    assert s.theme == "amr-query"
    log = journal.path_for(config, today()).read_text()
    assert "条件の比べ方どうしよう" in log and "4条件で比べる" in log


def test_empty_line_stops_the_speech(session):
    s, codex, speaker = session
    s.handle("")
    assert speaker.stopped == 1 and codex.asked == []


def test_new_conversation_forgets_the_session(session):
    s, codex, _ = session
    assert "新しく話そう" in s.handle("新しく話そう")
    assert codex.forgotten == 1


def test_decision_is_recorded_in_slack_through_kei_agent(session, config):
    s, codex, _ = session
    s.theme = "amr-query"
    codex.replies = [_turn("じゃあそれで進めよう。\n📌 決定: moments を正解として使う")]

    s.handle("これで決めていい？")

    asks = [payload for _, payload in ask.pending_asks(config)]
    assert asks == [{"theme": "amr-query", "text": "moments を正解として使う", "kind": "note",
                     "created_at": pytest.approx(time.time(), abs=10)}]


def test_request_is_read_back_before_it_is_sent(session, config):
    s, codex, speaker = session
    s.theme = "amr-query"
    codex.replies = [_turn("やっておくね。\n🛠 依頼: 条件ごとの精度を集計して")]

    said = s.handle("さっきの集計やっといて")
    assert "いい？" in said and ask.pending_asks(config) == []      # まだ渡さない

    s.say(said)
    sent = s.handle("いいよ")
    assert "渡したよ" in sent
    payload = ask.pending_asks(config)[0][1]
    assert payload["text"] == "条件ごとの精度を集計して" and payload["kind"] == "request"


def test_unclear_answer_does_not_send_the_request(session, config):
    s, codex, _ = session
    s.theme = "amr-query"
    codex.replies = [_turn("🛠 依頼: 集計して")]
    s.handle("やっといて")
    assert "やめておく" in s.handle("えーと")
    assert ask.pending_asks(config) == []


def test_request_can_be_cancelled(session, config):
    s, codex, _ = session
    s.theme = "amr-query"
    codex.replies = [_turn("🛠 依頼: 全部消して")]
    s.handle("やっといて")
    assert "やめておく" in s.handle("やめて")
    assert ask.pending_asks(config) == []


def test_request_without_a_theme_asks_for_one(session, config):
    s, codex, _ = session
    codex.replies = [_turn("🛠 依頼: 集計して")]
    assert "テーマ" in s.handle("やっといて")
    assert ask.pending_asks(config) == []


def test_usage_limit_is_explained_with_the_reset_time(session):
    s, codex, _ = session
    codex.replies = [_turn(failed="You've hit your usage limit ... try again at 6:01 PM.", limit_reset="6:01 PM")]
    said = s.handle("どう思う？")
    assert "上限" in said and "6:01 PM" in said and "Slack の Kei Agent" in said


# 終わったことの知らせ

def test_finished_work_is_announced_once(session, config, store):
    s, codex, speaker = session
    run = store.start_run("C1", "1.1", "amr-query", "voice")
    store.end_run(run, False, None)
    s.watcher = bridge.Watcher(config, since=0)

    s.announce_finished()
    s.announce_finished()

    spoken = "".join(speaker.said)
    assert spoken.count("終わったよ") == 1 and "結果は Slack に出てる" in spoken


def test_failed_work_is_announced_as_failed(session, config, store):
    s, codex, speaker = session
    run = store.start_run("C1", "1.1", "amr-query", "voice")
    store.end_run(run, True, None)
    s.watcher = bridge.Watcher(config, since=0)

    s.announce_finished()

    assert any("うまくいかなかった" in t for t in speaker.said)


# Codex の呼び方

def test_deep_thinking_is_asked_for_by_words():
    assert wants_deep("じっくり考えてみて") and not wants_deep("どう思う？")


def test_command_resumes_the_same_conversation(tmp_path):
    codex = Codex(cwd=tmp_path, state_dir=tmp_path / "voice")
    first = codex.build_command(None, deep=False)
    assert "resume" not in first and "model_reasoning_effort=low" in first

    codex.remember("2026-09-19", "abc-123")
    assert codex.session_id("2026-09-19") == "abc-123"
    assert codex.session_id("2026-09-20") is None      # 日が変わったら新しい会話

    resumed = codex.build_command("abc-123", deep=True)
    assert resumed[2:4] == ["resume", "abc-123"] and "model_reasoning_effort=high" in resumed


def test_ask_reads_the_last_message_and_the_stream(tmp_path):
    """codex exec の代わりに、出来事と最後の返事を出す小さなコマンドを使う。"""
    fake = tmp_path / "fake-codex.py"
    fake.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('-o') + 1]\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'sess-9'}))\n"
        "print(json.dumps({'type': 'item.completed',"
        " 'item': {'type': 'agent_message', 'text': '前半だよ。'}}))\n"
        "open(out, 'w').write('前半だよ。後半もね。')\n",
        encoding="utf-8",
    )
    codex = Codex(cwd=tmp_path, state_dir=tmp_path / "voice", instructions="決まり",
                  command=(sys_executable(), str(fake)))
    spoken: list[str] = []

    turn = codex.ask("どう思う？", "2026-09-19", on_text=spoken.append)

    assert turn.text == "前半だよ。後半もね。" and spoken == ["前半だよ。"]
    assert turn.session_id == "sess-9"
    assert json.loads((tmp_path / "voice" / "session.json").read_text())["session_id"] == "sess-9"


def test_ask_reports_the_usage_limit(tmp_path):
    fake = tmp_path / "limited.py"
    fake.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'turn.failed', 'error': {'message':"
        " \"You've hit your usage limit. ... try again at 6:01 PM.\"}}))\n",
        encoding="utf-8",
    )
    codex = Codex(cwd=tmp_path, state_dir=tmp_path / "voice", command=(sys_executable(), str(fake)))
    turn = codex.ask("どう思う？", "2026-09-19")
    assert turn.limit_reset == "6:01 PM" and turn.failed


def sys_executable() -> str:
    import sys
    return sys.executable


def test_journal_keeps_the_whole_conversation(config):
    journal.append(config, "2026-09-19", "依頼者", "こんにちは")
    path = journal.append(config, "2026-09-19", "Kei Agent（声）", "やあ")
    text = path.read_text()
    assert path == config.research_root / "_overview" / "voice" / "2026-09-19.md"
    assert "## 依頼者" in text and "## Kei Agent（声）" in text and "やあ" in text
    assert isinstance(path, Path)
