"""Moodle の締切を Notion の「課題」に取り込むところ。"""

from datetime import datetime

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent_course import notion_sync
from kei_agent_course.ics import Event

STATE = {"databases": {"courses": {"data_source_id": "ds-courses"},
                       "assignments": {"data_source_id": "ds-assignments"}}}

REPORT = Event(uid="2345678@moodle", summary="第3回レポート の 提出期限",
               starts_at=datetime(2026, 9, 25, 23, 59), course="データベース(2019ZZ26)",
               url="https://wsdmoodle.waseda.jp/mod/assign/view.php?id=12345")


def _title(text):
    return {"title": [{"plain_text": text}]}


def _rich(text):
    return {"rich_text": [{"plain_text": text}]}


def _row(event, page_id="row-1", course_page="page-db", when=None):
    return {"id": page_id, "properties": {
        "タイトル": _title(event.summary),
        "Moodle ID": _rich(event.uid),
        "締切": {"date": {"start": when or event.starts_at.astimezone().isoformat()}},
        "Moodle": {"url": event.url},
        "科目": {"relation": [{"id": course_page}]},
    }}


class FakeNotion:
    def __init__(self, courses=(), assignments=()):
        self.rows = {"ds-courses": list(courses), "ds-assignments": list(assignments)}
        self.calls = []

    def paginate(self, method, path, body=None):
        return self.rows[path.split("/")[2]]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"id": "new-row"}


COURSE_ROWS = [{"id": "page-db", "properties": {"科目名": _title("データベース")}}]


def _sync(notion, events, known_only=True):
    return notion_sync.CourseNotion(notion, STATE).sync(events, known_only=known_only)


def test_a_new_deadline_becomes_a_row():
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, [REPORT])

    (method, path, body), = notion.calls
    assert (method, path) == ("POST", "/pages")
    props = body["properties"]
    assert props["タイトル"]["title"][0]["text"]["content"] == "第3回レポート の 提出期限"
    # 手元の時刻に時差を付けて渡す（Notion 側でずれない）
    assert props["締切"]["date"]["start"] == REPORT.starts_at.astimezone().isoformat()
    assert props["出どころ"]["select"]["name"] == "Moodle"
    assert props["Moodle ID"]["rich_text"][0]["text"]["content"] == REPORT.uid
    # 科目名は履修コードを外して「授業」と突き合わせる
    assert props["科目"]["relation"] == [{"id": "page-db"}]
    assert props["状態"]["status"]["name"] == "未着手"
    assert len(result.added) == 1 and result.unchanged == 0


def test_the_same_deadline_is_left_alone():
    notion = FakeNotion(courses=COURSE_ROWS, assignments=[_row(REPORT)])
    result = _sync(notion, [REPORT])

    assert notion.calls == []
    assert result.unchanged == 1 and not result.added and not result.updated


def test_a_moved_deadline_updates_the_same_row():
    """締切が変わったら、同じ行を直す（新しい行を作らない）。状態や見積時間には触らない。"""
    old = _row(REPORT, when="2026-09-20T23:59:00+09:00")
    notion = FakeNotion(courses=COURSE_ROWS, assignments=[old])
    result = _sync(notion, [REPORT])

    (method, path, body), = notion.calls
    assert (method, path) == ("PATCH", "/pages/row-1")
    assert "状態" not in body["properties"] and "見積時間" not in body["properties"]
    assert len(result.updated) == 1


OTHER = Event(uid="9@moodle", summary="小テスト の 終了日時",
              starts_at=datetime(2026, 10, 1, 23, 59), course="新入生セミナー(2019ZZ9S)")


def test_a_course_that_is_not_taken_is_skipped():
    """Moodle のカレンダーには履修していない科目も並ぶので、既定では入れずに名前だけ知らせる。"""
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, [OTHER])

    assert notion.calls == []
    assert result.other_courses == ["新入生セミナー"] and not result.added
    assert "履修していない科目" in result.summary()


def test_all_courses_can_be_taken_with_an_empty_relation():
    """全部入れると言われたときは、科目の欄を空にして入れる。"""
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, [OTHER], known_only=False)

    (_, _, body), = notion.calls
    assert "科目" not in body["properties"]
    assert len(result.added) == 1 and result.other_courses == ["新入生セミナー"]


def test_it_stops_before_writing_too_many_rows(monkeypatch):
    """ics を読み違えても、一度に作りすぎない。"""
    monkeypatch.setattr(notion_sync, "MAX_WRITES", 2)
    events = [Event(uid=f"{i}@moodle", summary=f"課題{i} の 提出期限",
                    starts_at=datetime(2026, 10, 1, 23, 59), course="データベース") for i in range(5)]
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, events)

    assert len(notion.calls) == 2 and result.stopped
    assert "止めた" in result.summary()


def test_state_file_missing_says_what_to_run(tmp_path):
    with pytest.raises(notion_sync.SyncError, match="kei-agent-course-setup"):
        notion_sync.read_state(tmp_path / "notion-course.json")


# 履修中の科目（朝のまとめで、時限を時刻に直すのに使う）


def _select(name):
    return {"select": {"name": name}}


COURSE_ROWS_FULL = [
    {"id": "p1", "url": "https://notion/p1", "properties": {
        "科目名": _title("データベース"), "曜日": _select("月"), "時限": {"number": 2}, "状態": _select("履修中")}},
    {"id": "p2", "url": "https://notion/p2", "properties": {
        "科目名": _title("次世代ネットワーク"), "曜日": _select("金"), "時限": {"number": 4}, "状態": _select("履修中")}},
    {"id": "p3", "url": "https://notion/p3", "properties": {
        "科目名": _title("去年の科目"), "曜日": _select("月"), "時限": {"number": 1}, "状態": _select("終了")}},
    {"id": "p4", "url": "https://notion/p4", "properties": {
        "科目名": _title("プロジェクト研究B"), "曜日": _select("他"), "時限": {"number": None},
        "状態": _select("履修中")}},
]


def test_courses_on_takes_only_the_courses_being_taken():
    notion = FakeNotion(courses=COURSE_ROWS_FULL)
    found = notion_sync.CourseNotion(notion, STATE).courses_on("月")
    assert [c["subject"] for c in found] == ["データベース"]   # 終了した科目は出さない


def test_courses_on_without_a_weekday_returns_all_and_sorts_by_period():
    notion = FakeNotion(courses=COURSE_ROWS_FULL)
    found = notion_sync.CourseNotion(notion, STATE).courses_on()
    # 時限の早い順。時限のないもの（集中講義など）は最後
    assert [c["subject"] for c in found] == ["データベース", "次世代ネットワーク", "プロジェクト研究B"]


def test_waseda_periods_turn_into_times():
    from datetime import date

    from kei_agent_course import periods

    assert periods.weekday_of(date(2026, 9, 21)) == "月"
    start, end = periods.at(date(2026, 9, 21), 2)
    assert (start.hour, start.minute) == (10, 40) and (end.hour, end.minute) == (12, 20)
    assert periods.at(date(2026, 9, 21), None) is None      # 時限なし（集中講義）
    assert periods.at(date(2026, 9, 21), 9) is None         # 無い時限
