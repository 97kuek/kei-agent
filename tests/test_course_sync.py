"""Moodle の締切を Notion の「課題」に取り込むところ。"""

import json

from datetime import datetime

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent_course import notion_sync
from kei_agent_course.ics import Event

STATE = {"home_page_id": "course-home", "databases": {
    "courses": {"data_source_id": "ds-courses"},
    "assignments": {"data_source_id": "ds-assignments"},
    "study_logs": {"data_source_id": "ds-study-logs"},
}}

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
    def __init__(self, courses=(), assignments=(), study_logs=()):
        self.rows = {"ds-courses": list(courses), "ds-assignments": list(assignments),
                     "ds-study-logs": list(study_logs)}
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


def test_read_state_rejects_legacy_three_database_state(tmp_path):
    state = {"databases": {key: {} for key in ("courses", "assignments", "study_logs")}}
    path = tmp_path / "notion-course.json"
    path.write_text(json.dumps(state))

    with pytest.raises(notion_sync.SyncError, match="kei-agent-course-setup"):
        notion_sync.read_state(path)


# 履修中の科目（朝のまとめで、時限を時刻に直すのに使う）


def _select(name):
    return {"select": {"name": name}}


COURSE_ROWS_FULL = [
    {"id": "p1", "url": "https://notion/p1", "properties": {
        "科目名": _title("データベース"), "曜日": _select("月"), "時限": {"number": 2},
        "状態": _select("履修中"), "学期": _select("秋学期")}},
    {"id": "p2", "url": "https://notion/p2", "properties": {
        "科目名": _title("次世代ネットワーク"), "曜日": _select("金"), "時限": {"number": 4},
        "状態": _select("履修中"), "学期": _select("秋学期")}},
    {"id": "p3", "url": "https://notion/p3", "properties": {
        "科目名": _title("去年の科目"), "曜日": _select("月"), "時限": {"number": 1}, "状態": _select("終了")}},
    {"id": "p4", "url": "https://notion/p4", "properties": {
        "科目名": _title("プロジェクト研究B"), "曜日": _select("他"), "時限": {"number": None},
        "状態": _select("履修中"), "学期": _select("秋学期")}},
]


def test_courses_on_takes_only_the_courses_being_taken():
    from datetime import date

    notion = FakeNotion(courses=COURSE_ROWS_FULL)
    found = notion_sync.CourseNotion(notion, STATE).courses_on("月", date(2026, 9, 21))
    assert [c["subject"] for c in found] == ["データベース"]   # 終了した科目は出さない


def test_courses_on_without_a_weekday_returns_all_and_sorts_by_period():
    from datetime import date

    notion = FakeNotion(courses=COURSE_ROWS_FULL)
    found = notion_sync.CourseNotion(notion, STATE).courses_on(on=date(2026, 9, 21))
    # 時限の早い順。時限のないもの（集中講義など）は最後
    assert [c["subject"] for c in found] == ["データベース", "次世代ネットワーク", "プロジェクト研究B"]


def test_current_courses_returns_only_this_terms_enrolled_courses():
    from datetime import date

    found = notion_sync.CourseNotion(FakeNotion(courses=COURSE_ROWS_FULL), STATE).current_courses(date(2026, 9, 21))

    assert [c["subject"] for c in found] == ["データベース", "次世代ネットワーク", "プロジェクト研究B"]


def test_match_course_keeps_finished_course_out_of_schedule_but_available_for_grades():
    from datetime import date

    rows = [*COURSE_ROWS_FULL, {"id": "math-page", "properties": {
        "科目名": _title("数学"), "年度": {"number": 2025}, "学期": _select("春学期"),
        "状態": _select("終了"),
    }}]
    course = notion_sync.CourseNotion(FakeNotion(courses=rows), STATE)

    assert "数学" not in [item["subject"] for item in course.current_courses(date(2026, 9, 21))]
    assert course.match_course("数学", 2025, "春期") == "math-page"


def test_study_time_log_is_idempotent_and_relates_the_course():
    notion = FakeNotion(courses=COURSE_ROWS)
    course = notion_sync.CourseNotion(notion, STATE)

    first = course.record_study_time("entry-1", "2026-09-23T10:00:00+09:00", 25, "page-db", "復習",
                                     "https://slack.example/p1")
    second = course.record_study_time("entry-1", "2026-09-23T10:00:00+09:00", 25, "page-db", "復習",
                                      "https://slack.example/p1")

    assert first == second == {"entry_id": "entry-1", "notion_url": ""}
    posts = [call for call in notion.calls if call[:2] == ("POST", "/pages")]
    assert len(posts) == 1
    props = posts[0][2]["properties"]
    assert props["科目"]["relation"] == [{"id": "page-db"}]
    assert props["Kei Agent 記録ID"]["rich_text"][0]["text"]["content"] == "entry-1"


def test_waseda_periods_turn_into_times():
    from datetime import date

    from kei_agent_course import periods

    assert periods.weekday_of(date(2026, 9, 21)) == "月"
    start, end = periods.at(date(2026, 9, 21), 2)
    assert (start.hour, start.minute) == (10, 40) and (end.hour, end.minute) == (12, 20)
    assert periods.at(date(2026, 9, 21), None) is None      # 時限なし（集中講義）
    assert periods.at(date(2026, 9, 21), 9) is None         # 無い時限


def test_courses_on_drops_the_other_term():
    """学期の終わった科目が「履修中」で残っていても、今の学期のものだけを返す。"""
    from datetime import date

    rows = COURSE_ROWS_FULL + [
        {"id": "p5", "url": "", "properties": {
            "科目名": _title("春の科目"), "曜日": _select("月"), "時限": {"number": 3},
            "状態": _select("履修中"), "学期": _select("春学期")}},
        {"id": "p6", "url": "", "properties": {
            "科目名": _title("通年の科目"), "曜日": _select("月"), "時限": {"number": 5},
            "状態": _select("履修中"), "学期": _select("通年")}},
    ]
    notion = FakeNotion(courses=rows)
    autumn = notion_sync.CourseNotion(notion, STATE).courses_on("月", date(2026, 9, 21))
    assert [c["subject"] for c in autumn] == ["データベース", "通年の科目"]

    notion = FakeNotion(courses=rows)
    spring = notion_sync.CourseNotion(notion, STATE).courses_on("月", date(2026, 5, 11))
    assert [c["subject"] for c in spring] == ["春の科目", "通年の科目"]


def test_a_course_without_a_term_is_kept():
    """学期が空の科目は、隠すより出す（見落としのほうが困る）。"""
    from datetime import date

    rows = [{"id": "p9", "url": "", "properties": {
        "科目名": _title("学期なし"), "曜日": _select("月"), "時限": {"number": 2}, "状態": _select("履修中")}}]
    found = notion_sync.CourseNotion(FakeNotion(courses=rows), STATE).courses_on("月", date(2026, 5, 11))
    assert [c["subject"] for c in found] == ["学期なし"]


def test_course_tools_read_box_and_fully_manage_the_course_notion():
    """Box は読むだけ。Notion は授業ホームの中で全部できる（範囲は Notion 側の共有で縛る）。"""
    from kei_agent_course import tools

    assert all(name.startswith(("mcp__claude_ai_Box__", "mcp__claude_ai_Notion__")) for name in tools.ALLOWED)
    box = [n for n in tools.ALLOWED if "Box" in n]
    assert not [n for n in box if any(w in n for w in ("upload", "create", "update", "move", "copy", "set_"))]
    # 「提出済みにして」も「このページを移して」も頼める
    for name in ("mcp__claude_ai_Notion__notion-update-page", "mcp__claude_ai_Notion__notion-move-pages",
                 "mcp__claude_ai_Notion__notion-duplicate-page", "mcp__claude_ai_Notion__notion-create-database"):
        assert name in tools.ALLOWED
    # Box への書き込みと、別のエージェントを動かす道具は、名指しでも断る
    for name in ("mcp__claude_ai_Box__upload_file", "mcp__claude_ai_Notion__notion-spawn-session"):
        assert name in tools.DENY and name not in tools.ALLOWED
