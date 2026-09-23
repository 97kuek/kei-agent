"""Toggl の記録を科目ごとに集計するところ（大学エージェント）。"""

from datetime import date

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent_course import toggl_report


def entry(project, description, hours, **extra):
    return {"project": {"name": project}, "description": description,
            "duration": hours * 3600, "start": "2026-09-18T10:00:00Z", **extra}


ENTRIES = [
    entry("データベース", "第3回レポート", 2),
    entry("データベース", "第3回レポート", 1),
    entry("データベース", "", 0.5),
    entry("情報セキュリティB", "小テスト", 1),
    entry("アルバイト", "レジ", 5),
    entry("データベース", "止め忘れ", -1),                    # 動かしっぱなしは数えない
    entry("データベース", "休憩", 1, type="break"),            # 休憩も数えない
    entry("データベース", "消した記録", 1, deleted_at="2026-09-18T12:00:00Z"),
]


def test_totals_adds_up_by_course_and_task():
    by_course = toggl_report.totals(ENTRIES)
    assert by_course["データベース"]["第3回レポート"] == 3 * 3600
    assert by_course["データベース"][toggl_report.NO_NAME] == 0.5 * 3600
    assert "止め忘れ" not in by_course["データベース"] and "休憩" not in by_course["データベース"]
    assert "消した記録" not in by_course["データベース"]


def test_only_the_courses_in_notion_are_counted():
    """アルバイトや個人開発のプロジェクトは数えない（「授業」にある科目だけ）。"""
    by_course = toggl_report.totals(ENTRIES, courses={"データベース", "情報セキュリティB"})
    assert set(by_course) == {"データベース", "情報セキュリティB"}


def test_totals_normalizes_a_university_project_prefix():
    entries = [{"duration": 1800, "type": "activity", "project": {"name": "大学 / データベース"}}]

    by_course = toggl_report.totals(entries, courses={"データベース"})

    assert by_course == {"データベース": {toggl_report.NO_NAME: 1800.0}}


def test_lines_put_the_longest_course_first():
    text = "\n".join(toggl_report.lines(
        toggl_report.totals(ENTRIES, courses={"データベース", "情報セキュリティB"}),
        date(2026, 9, 14), date(2026, 9, 20)))
    assert text.splitlines()[0] == "9/14〜9/20 の実績（Toggl）"
    assert text.splitlines()[1] == "• データベース: 3.5 時間"
    assert "    - 第3回レポート: 3.0 時間" in text
    assert text.splitlines()[-1] == "合計: 4.5 時間"


def test_lines_say_what_to_do_when_there_is_nothing():
    text = "\n".join(toggl_report.lines({}, date(2026, 9, 14), date(2026, 9, 20)))
    assert "記録がなかったよ" in text and "プロジェクト名" in text


def test_report_reads_the_last_seven_days(monkeypatch):
    asked = {}

    class _Toggl:
        def entries(self, since, until):
            asked["span"] = (since, until)
            return ENTRIES

    text = toggl_report.report(days=7, courses={"データベース"}, toggl=_Toggl(), today=date(2026, 9, 20))
    assert asked["span"] == (date(2026, 9, 14), date(2026, 9, 20))
    assert "データベース" in text and "アルバイト" not in text


def test_report_without_the_keys_says_so(monkeypatch):
    """鍵がなければ、何を入れればよいかを返す（黙って0時間にしない）。"""
    from kei_agent.timelog import TogglError

    monkeypatch.setattr(toggl_report, "load_toggl", lambda: None)
    with pytest.raises(TogglError, match="TOGGL_API_TOKEN"):
        toggl_report.report(days=7, today=date(2026, 9, 20))
