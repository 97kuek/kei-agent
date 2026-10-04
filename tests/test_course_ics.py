"""Moodle のカレンダー書き出し（ics）の読み取り。"""

from datetime import date, datetime

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent_modules.course import ics, moodle, notion_setup, school

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024100700//EN
BEGIN:VEVENT
UID:2345678@wsdmoodle.waseda.jp
SUMMARY:第3回レポート の 提出期限
DESCRIPTION:PDF で提出すること\\n提出先は Moodle
CATEGORIES:情報理論
URL:https://wsdmoodle.waseda.jp/mod/assign/view.php?id=12345
DTSTART:20260925T145900Z
DTSTAMP:20260901T000000Z
END:VEVENT
BEGIN:VEVENT
UID:2345679@wsdmoodle.waseda.jp
SUMMARY:小テスト の 終了日時
CATEGORIES:自然言語処理
DTSTART;VALUE=DATE:20260930
END:VEVENT
BEGIN:VEVENT
UID:9999@wsdmoodle.waseda.jp
SUMMARY:学部ガイダンス
CATEGORIES:お知らせ
DTSTART:20260922T010000Z
END:VEVENT
BEGIN:VEVENT
UID:3333@wsdmoodle.waseda.jp
SUMMARY:【ミニテスト】著作権 の受験可能期間開始
CATEGORIES:新入生セミナー(2019ZZ9S00000135)
DTSTART:20260924T000000Z
END:VEVENT
BEGIN:VEVENT
UID:4444@wsdmoodle.waseda.jp
SUMMARY:【ミニテスト】著作権 の受験可能期間終了
CATEGORIES:新入生セミナー(2019ZZ9S00000135)
DTSTART:20260929T145900Z
END:VEVENT
BEGIN:VEVENT
UID:1111@wsdmoodle.waseda.jp
SUMMARY:第1回レポート の 提出期限
CATEGORIES:情報理論
DTSTART:20260410T145900Z
END:VEVENT
END:VCALENDAR
"""


def test_unfold_joins_wrapped_lines():
    assert ics.unfold("SUMMARY:とても長い課\n 題の名前\nUID:1") == ["SUMMARY:とても長い課題の名前", "UID:1"]


def test_parse_reads_the_fields_we_use():
    events = ics.parse(SAMPLE)
    first = events[0]
    assert first.summary == "第3回レポート の 提出期限"
    assert first.course == "情報理論"
    assert first.description == "PDF で提出すること\n提出先は Moodle"   # \\n は改行に戻す
    assert first.url.endswith("id=12345")
    # 書き出しは UTC。手元の時刻（JST）に直して読む
    assert first.starts_at == datetime(2026, 9, 25, 23, 59)
    # 日付だけの予定は、その日の終わり
    assert events[1].starts_at == datetime(2026, 9, 30, 23, 59)
    # 科目名から履修のコードを外す
    assert next(e for e in events if e.uid.startswith("4444")).course_name == "新入生セミナー"
    kinds = {e.summary: e.kind for e in events}
    assert kinds["【ミニテスト】著作権 の受験可能期間開始"] == "start"
    assert kinds["【ミニテスト】著作権 の受験可能期間終了"] == "due"
    assert kinds["学部ガイダンス"] == "other"


def test_due_events_drops_the_past_and_the_not_due_and_respects_the_window():
    found = ics.due_events(SAMPLE, since=date(2026, 9, 20))
    # ガイダンス（締切ではない）、受付の開始、4月の課題（過ぎている）は落とす
    assert [e.summary for e in found] == [
        "第3回レポート の 提出期限", "【ミニテスト】著作権 の受験可能期間終了", "小テスト の 終了日時"]
    assert ics.due_events(SAMPLE, since=date(2026, 9, 20), days=4) == []      # 9/24 まで
    assert len(ics.due_events(SAMPLE, since=date(2026, 9, 20), days=6)) == 1  # 9/26 まで


# カレンダーの取得（Moodle）


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_rejects_a_page_that_is_not_a_calendar(monkeypatch):
    monkeypatch.setattr(moodle.urllib.request, "urlopen", lambda *a, **kw: _Resp(b"<html>login</html>"))
    with pytest.raises(moodle.MoodleError, match="ics ではありません"):
        moodle.fetch("https://example.invalid/calendar.ics")


def test_due_reads_the_calendar(monkeypatch):
    monkeypatch.setattr(moodle, "fetch", lambda url, timeout=30: SAMPLE)
    found = moodle.due("https://example.invalid/calendar.ics", since=date(2026, 9, 20))
    assert [e.course_name for e in found] == ["情報理論", "新入生セミナー", "自然言語処理"]


def _event(summary):
    return ics.Event(uid="1", summary=summary, starts_at=None)


@pytest.mark.parametrize(("summary", "kind"), [
    # 名前の途中に「開始」があっても、末尾が締切の言い回しなら締切
    ("開始前アンケート の 終了", "due"),
    ("「開始報告」の提出期限", "due"),
    ("Quiz 1 opens", "start"),
    ("Quiz 1 closes", "due"),
    ("Assignment is due", "due"),
    # 英単語は単語の区切りで見る（procedure の due、weekends の ends に当てない）
    ("Procedure review", "other"),
    ("Weekends seminar", "other"),
])
def test_kind_checks_the_due_suffix_first_and_english_words_by_boundary(summary, kind):
    assert _event(summary).kind == kind


def test_due_events_without_since_starts_from_now(monkeypatch):
    """今日のうちでも、もう過ぎた締切は入れない。"""
    text = SAMPLE.replace("DTSTART:20260925T145900Z", "DTSTART:20260925T010000Z")   # 9/25 10:00 JST

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 12, 0)

    class _Today(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 25)

    monkeypatch.setattr(ics, "datetime", _Now)
    monkeypatch.setattr(ics, "date", _Today)
    found = ics.due_events(text, days=7)
    assert "第3回レポート の 提出期限" not in [e.summary for e in found]
    assert "【ミニテスト】著作権 の受験可能期間終了" in [e.summary for e in found]


def test_fetch_unfolds_before_decoding_so_a_split_character_survives(monkeypatch):
    """折り返しは75バイトごと。日本語の1文字の途中で折り返されても、文字化けさせない。"""
    word = "課題".encode()
    body = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:" + word[:2] + b"\r\n " + word[2:]
            + b"\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    monkeypatch.setattr(moodle.urllib.request, "urlopen", lambda *a, **kw: _Resp(body))
    assert ics.parse(moodle.fetch("https://example.invalid/calendar.ics"))[0].summary == "課題"


def test_add_course_writes_the_academic_year_and_the_seed_file(tmp_path, monkeypatch):
    """年度が無いと、次の年も「履修中」の科目として出てしまう。履修科目は、自分のフォルダのファイルから入れる。"""
    posts = []

    class _Notion:
        def paginate(self, _method, _path, _body):
            # 去年の同名科目は、今年の科目とは別に足す
            return [{"id": "old", "properties": {"科目名": {"title": [{"plain_text": "データベース"}]},
                                                  "年度": {"number": 2025}}}]

        def request(self, method, path, body=None):
            posts.append(body)
            return {}

    setup = notion_setup.CourseSetup(_Notion(), "home", tmp_path / "notion-course.json")
    setup.state = {"databases": {"courses": {"data_source_id": "ds"}}}
    setup.add_course("データベース", "月", 2, year=2026)
    assert posts[0]["properties"]["年度"] == {"number": 2026}

    seed = tmp_path / "courses.toml"
    seed.write_text('year = 2027\nterm = "春学期"\n[[courses]]\nname = "英語"\nweekday = "火"\nperiod = 1\n'
                    '[[courses]]\nname = "卒業研究"\nweekday = "他"\nterm = "通年"\n', encoding="utf-8")
    seeded, clients = [], []
    fake_config = type("C", (), {"state_dir": str(tmp_path), "user_dir": None,
                                 "notion": type("N", (), {"course_home": "course-home"})(),
                                 "settings": lambda self, name: {"school": "waseda"}})()
    monkeypatch.setattr(notion_setup, "load_config", lambda: fake_config)
    monkeypatch.setattr(notion_setup, "gateway_notion",
                        lambda client, config=None: clients.append(client) or _Notion())
    monkeypatch.setattr(notion_setup.CourseSetup, "run",
                        lambda self, courses=None, year=None: seeded.append((self.home, year, courses, self.school)))
    notion_setup.main(["--seed", str(seed)])
    (home, year, courses, chosen), = seeded
    assert (home, year) == ("course-home", 2027) and chosen.name == "waseda"
    assert courses == [notion_setup.Course("英語", "火", 1, "春学期"), notion_setup.Course("卒業研究", "他", None, "通年")]
    # Notion にはゲートウェイの course（授業ホームだけに届く）として届く
    assert clients == ["course"]

    # 学期を省いた科目は、今日の学期（学校の設定から）
    setup = notion_setup.CourseSetup(_Notion(), "home", tmp_path / "notion-course.json", school.load({"school": "waseda"}))
    setup.state = {"databases": {"courses": {"data_source_id": "ds"}}}
    posts.clear()
    setup.add_course("統計", "水", 3, year=2026)
    assert posts[0]["properties"]["学期"]["select"]["name"] in ("春学期", "秋学期")


@pytest.mark.parametrize(("text", "message"), [
    ('year = 2026\n', "courses"),
    ('year = "2026"\n[[courses]]\nname = "英語"\n', "year"),
    ('[[courses]]\nname = "英語"\nweekday = "Mon"\n', "1 件目"),
    ('[[courses]]\nname = "英語"\nperiod = "1"\n', "1 件目"),
    ('[[courses]]\nweekday = "月"\n', "1 件目"),
])
def test_a_broken_seed_file_says_what_is_wrong(tmp_path, text, message):
    seed = tmp_path / "courses.toml"
    seed.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        notion_setup.read_seed(seed)


def test_the_example_seed_file_can_be_read():
    year, courses = notion_setup.read_seed(notion_setup.Path(notion_setup.__file__).parent / "courses.example.toml")
    assert year == 2026 and courses[0] == notion_setup.Course("データベース", "月", 2, "秋学期")
    assert courses[-1].term == "秋ク"


def test_course_setup_without_the_gateway_password_says_so(tmp_path, monkeypatch, config):
    monkeypatch.setattr(notion_setup, "load_config", lambda: config)
    with pytest.raises(SystemExit, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        notion_setup.main(["course-home"])
