"""Moodle のカレンダー書き出し（ics）の読み取り。"""

from datetime import date, datetime

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent_course import ics

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
    first = ics.parse(SAMPLE)[0]
    assert first.summary == "第3回レポート の 提出期限"
    assert first.course == "情報理論"
    assert first.description == "PDF で提出すること\n提出先は Moodle"   # \\n は改行に戻す
    assert first.url.endswith("id=12345")
    # 書き出しは UTC。手元の時刻（JST）に直して読む
    assert first.starts_at == datetime(2026, 9, 25, 23, 59)


def test_date_only_events_land_at_the_end_of_that_day():
    quiz = ics.parse(SAMPLE)[1]
    assert quiz.starts_at == datetime(2026, 9, 30, 23, 59)


def test_due_events_drops_the_past_and_the_not_due():
    found = ics.due_events(SAMPLE, since=date(2026, 9, 20))
    assert [e.summary for e in found] == [
        "第3回レポート の 提出期限", "【ミニテスト】著作権 の受験可能期間終了", "小テスト の 終了日時"]
    # ガイダンス（締切ではない）、受付の開始、4月の課題（過ぎている）は落とす


def test_kind_tells_deadlines_from_openings():
    kinds = {e.summary: e.kind for e in ics.parse(SAMPLE)}
    assert kinds["【ミニテスト】著作権 の受験可能期間開始"] == "start"
    assert kinds["【ミニテスト】著作権 の受験可能期間終了"] == "due"
    assert kinds["学部ガイダンス"] == "other"


def test_course_name_drops_the_enrolment_code():
    event = next(e for e in ics.parse(SAMPLE) if e.uid.startswith("4444"))
    assert event.course_name == "新入生セミナー"


def test_due_events_respects_the_window():
    assert ics.due_events(SAMPLE, since=date(2026, 9, 20), days=4) == []      # 9/24 まで
    assert len(ics.due_events(SAMPLE, since=date(2026, 9, 20), days=6)) == 1  # 9/26 まで


# カレンダーの取得（Moodle）


def test_fetch_rejects_a_page_that_is_not_a_calendar(monkeypatch):
    from kei_agent_course import moodle

    class _Resp:
        def read(self):
            return b"<html>login</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(moodle.urllib.request, "urlopen", lambda *a, **kw: _Resp())
    with pytest.raises(moodle.MoodleError, match="ics ではありません"):
        moodle.fetch("https://example.invalid/calendar.ics")


def test_due_reads_the_calendar(monkeypatch):
    from kei_agent_course import moodle

    monkeypatch.setattr(moodle, "fetch", lambda url, timeout=30: SAMPLE)
    found = moodle.due("https://example.invalid/calendar.ics", since=date(2026, 9, 20))
    assert [e.course_name for e in found] == ["情報理論", "新入生セミナー", "自然言語処理"]


# 授業用 Notion のセットアップ


def test_course_setup_creates_six_canonical_databases_and_relations(tmp_path):
    """授業ホームの正本6 DBを作り、科目・成績を中心に relation を張る。"""
    from kei_agent_course import notion_setup

    calls = []

    class _Notion:
        def children(self, page_id):
            return []

        def paginate(self, _method, _path, _body):
            return []

        def request(self, method, path, body=None):
            calls.append((method, path, body))
            if method == "POST" and path == "/databases":
                return {"id": f"db-{body['title'][0]['text']['content']}"}
            if method == "GET" and path.startswith("/databases/"):
                return {"data_sources": [{"id": f"ds-{path.split('/')[-1]}"}], "url": "https://notion.so/x"}
            if method == "GET" and path.startswith("/data_sources/"):
                name = path.split("ds-db-")[-1]
                spec = next(spec for title, spec in notion_setup.SPECS.values() if title == name)
                props = {n: {"id": f"p{i}", "type": next(iter(v))} for i, (n, v) in enumerate(spec["properties"].items())}
                props.update({n: {"id": "rel", "type": "relation"} for n in spec.get("relations", {})})
                return {"properties": props}
            return {}

    setup = notion_setup.CourseSetup(_Notion(), "home-page", tmp_path / "notion-course.json")
    setup.run()

    setup.add_course("データベース", "月", 2)
    setup.add_course("統計解析実習", "他")
    added = [b["properties"] for m, p, b in calls if m == "POST" and p == "/pages"]
    assert [(p["曜日"]["select"]["name"], p["時限"]["number"]) for p in added] == [("月", 2), ("他", None)]

    created = [b["title"][0]["text"]["content"] for m, p, b in calls if m == "POST" and p == "/databases"]
    assert created == ["授業", "課題", "学習ログ", "📊 成績履歴", "🎓 単位要件", "📈 GPA推移"]
    assignments = next(b for m, p, b in calls if m == "POST" and p == "/databases"
                       and b["title"][0]["text"]["content"] == "課題")
    relation = assignments["initial_data_source"]["properties"]["科目"]["relation"]
    assert relation["data_source_id"] == "ds-db-授業" and relation["dual_property"]["synced_property_name"] == "課題"
    grades = next(b for m, p, b in calls if m == "POST" and p == "/databases"
                  and b["title"][0]["text"]["content"] == "📊 成績履歴")
    assert grades["initial_data_source"]["properties"]["授業"]["relation"]["data_source_id"] == "ds-db-授業"
    requirements = next(b for m, p, b in calls if m == "POST" and p == "/databases"
                        and b["title"][0]["text"]["content"] == "🎓 単位要件")
    assert requirements["initial_data_source"]["properties"]["算入成績"]["relation"]["data_source_id"] == "ds-db-📊 成績履歴"
    assert set(setup.state["databases"]) == {"courses", "assignments", "study_logs", "grades", "requirements", "gpa"}


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
    from kei_agent_course import moodle

    word = "課題".encode()
    body = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:" + word[:2] + b"\r\n " + word[2:]
            + b"\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(moodle.urllib.request, "urlopen", lambda *a, **kw: _Resp())
    assert ics.parse(moodle.fetch("https://example.invalid/calendar.ics"))[0].summary == "課題"


def test_add_course_writes_the_academic_year_and_seed_takes_a_year(tmp_path, monkeypatch):
    """年度が無いと、次の年も「履修中」の科目として出てしまう。"""
    from kei_agent_course import notion_setup

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

    seeded = []
    monkeypatch.setattr(notion_setup, "load_config", lambda: type("C", (), {"state_dir": str(tmp_path)})())
    monkeypatch.setattr(notion_setup.CourseSetup, "run", lambda self, courses=None, year=None: seeded.append(year))
    monkeypatch.setenv("NOTION_COURSE_TOKEN", "x")
    notion_setup.main(["home", "--seed", "2027"])
    assert seeded == [2027]
