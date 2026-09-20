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
    assert [e.summary for e in found] == ["第3回レポート の 提出期限", "小テスト の 終了日時"]
    # ガイダンス（締切ではない）と、4月の課題（過ぎている）は落とす


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
    assert [e.course for e in found] == ["情報理論", "自然言語処理"]
