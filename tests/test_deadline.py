"""締切の 0:00 ちょうどは、前の日の 24:00 と読む（Moodle の締切はこの形が多い）。"""

from datetime import date, datetime

from kei_agent import course, deadline, morning


def test_midnight_is_the_end_of_the_previous_day():
    assert deadline.day(datetime(2026, 10, 26, 0, 0)) == date(2026, 10, 25)
    assert deadline.clock(datetime(2026, 10, 26, 0, 0)) == "24:00"
    assert deadline.day(datetime(2026, 10, 26, 23, 59)) == date(2026, 10, 26)
    assert deadline.clock(datetime(2026, 10, 26, 9, 5)) == "09:05"


def test_the_morning_timeline_shows_tonights_midnight_deadline_at_24_00():
    now = datetime(2026, 10, 25, 7, 30)                       # 日曜の朝
    dues = [{"at": "2026-10-26T00:00:00+09:00", "title": "Assignment A", "course": "情報"},
            {"at": "2026-10-25T00:00:00+09:00", "title": "昨夜の課題", "course": "情報"}]

    lines = morning.text([], [], dues, now).splitlines()

    assert any(line.startswith("`24:00") and "情報 Assignment A" in line for line in lines)
    assert not any("昨夜の課題" in line for line in lines)      # 土曜の 24:00 は、もう過ぎている


def test_reminders_and_later_lines_use_the_previous_day():
    now = datetime(2026, 10, 24, 12, 0)
    item = {"id": "a", "at": "2026-10-26T00:00:00+09:00", "title": "Assignment A", "course": "情報"}

    assert "10/25（日） 24:00 まで" in course.soon_text(item, now)
    assert morning.later([item], now) == "このあとの締切: 10/25 Assignment A"
    assert "`10/25（日） 24:00` 情報 Assignment A" in morning.soon_deadlines([item], now, 2)
