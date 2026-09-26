"""Moodle の締切を Notion の「課題」に取り込むところ。"""

import json
from datetime import date, datetime

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent.dates import weekday
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
    return {"id": page_id, "url": f"https://notion.example/{page_id}", "properties": {
        "課題": _title(event.summary),
        "Moodle ID": _rich(event.uid),
        "締切": {"date": {"start": when or event.starts_at.astimezone().isoformat()}},
        "Moodle": {"url": event.url},
        "科目": {"relation": [{"id": course_page}]},
    }}


class FakeNotion:
    def __init__(self, courses=(), assignments=(), study_logs=(), page_children=None):
        self.rows = {"ds-courses": list(courses), "ds-assignments": list(assignments),
                     "ds-study-logs": list(study_logs)}
        self.calls = []
        self.page_children = dict(page_children or {})

    def paginate(self, method, path, body=None):
        return self.rows[path.split("/")[2]]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/blocks/") and path.endswith("/children"):
            page_id = path.split("/")[2]
            return {"results": self.page_children.get(page_id, [])}
        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            page_id = path.split("/")[2]
            self.page_children.setdefault(page_id, []).extend(body["children"])
            return {"results": body["children"]}
        return {"id": "new-row"}

    def appended_children(self, page_id):
        return [block["heading_2"]["rich_text"][0]["text"]["content"]
                for block in self.page_children.get(page_id, []) if block.get("type") == "heading_2"]


COURSE_ROWS = [{"id": "page-db", "properties": {"科目名": _title("データベース")}}]


def _sync(notion, events, known_only=True):
    return notion_sync.CourseNotion(notion, STATE).sync(events, known_only=known_only)


def _page_writes(notion):
    return [call for call in notion.calls if call[:2] == ("POST", "/pages")]


def test_calendar_assignment_snapshot_reads_all_notion_rows_including_manual():
    assignments = [_row(REPORT, page_id=f"p-{i}") for i in range(21)]
    assignments.append({"id": "manual", "url": "https://notion.example/manual", "properties": {
        "課題": _title("手入力の課題"), "締切": {"date": {"start": "2026-10-01T23:59:00+09:00"}},
        "状態": {"status": {"name": "未着手"}},
    }})
    notion = FakeNotion(assignments=assignments)

    snapshot = notion_sync.CourseNotion(notion, STATE).calendar_assignments(
        days=30, today=date(2026, 9, 24))

    assert snapshot["complete"] is True
    assert len(snapshot["items"]) == 22
    assert next(item for item in snapshot["items"] if item["id"] == "manual") == {
        "id": "manual", "title": "手入力の課題", "due": "2026-10-01T23:59:00+09:00",
        "status": "未着手", "url": "https://notion.example/manual"}
    assert notion.calls == []


def test_a_new_deadline_becomes_a_row():
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, [REPORT])

    (method, path, body), = _page_writes(notion)
    assert (method, path) == ("POST", "/pages")
    props = body["properties"]
    assert props["課題"]["title"][0]["text"]["content"] == "第3回レポート の 提出期限"
    # 手元の時刻に時差を付けて渡す（Notion 側でずれない）
    assert props["締切"]["date"]["start"] == REPORT.starts_at.astimezone().isoformat()
    assert props["出どころ"]["select"]["name"] == "Moodle"
    assert props["Moodle ID"]["rich_text"][0]["text"]["content"] == REPORT.uid
    # 科目名は履修コードを外して「授業」と突き合わせる
    assert props["科目"]["relation"] == [{"id": "page-db"}]
    assert props["状態"]["status"]["name"] == "未着手"
    assert len(result.added) == 1 and result.unchanged == 0


def test_assignment_title_removes_only_quoted_submission_suffix():
    assert notion_sync.assignment_title("「Assignment A」の提出期限") == "Assignment A"
    assert notion_sync.assignment_title("【ミニテスト】PC の受験可能期間の終了") == "【ミニテスト】PC の受験可能期間の終了"
    assert notion_sync.assignment_title("アンケート終了 ") == "アンケート終了 "
    assert notion_sync.assignment_title(" 「Assignment A」の提出期限") == " 「Assignment A」の提出期限"


def test_new_assignment_gets_sections_but_existing_page_body_is_preserved():
    notion = FakeNotion(courses=COURSE_ROWS, page_children={"new-row": []})
    _sync(notion, [REPORT])

    assert notion.appended_children("new-row") == ["やること", "提出物", "進捗メモ", "資料・リンク"]

    notion.page_children["existing-row"] = [{"type": "paragraph"}]
    notion_sync.CourseNotion(notion, STATE).ensure_assignment_template("existing-row")
    assert notion.appended_children("existing-row") == []


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

    (_, _, body), = _page_writes(notion)
    assert "科目" not in body["properties"]
    assert len(result.added) == 1 and result.other_courses == ["新入生セミナー"]


def test_it_stops_before_writing_too_many_rows(monkeypatch):
    """ics を読み違えても、一度に作りすぎない。"""
    monkeypatch.setattr(notion_sync, "MAX_WRITES", 2)
    events = [Event(uid=f"{i}@moodle", summary=f"課題{i} の 提出期限",
                    starts_at=datetime(2026, 10, 1, 23, 59), course="データベース") for i in range(5)]
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, events)

    assert len(_page_writes(notion)) == 2 and result.stopped
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


def test_finished_course_stays_out_of_the_schedule():
    from datetime import date

    rows = [*COURSE_ROWS_FULL, {"id": "math-page", "properties": {
        "科目名": _title("数学"), "年度": {"number": 2025}, "学期": _select("春学期"),
        "状態": _select("終了"),
    }}]
    course = notion_sync.CourseNotion(FakeNotion(courses=rows), STATE)

    assert "数学" not in [item["subject"] for item in course.current_courses(date(2026, 9, 21))]


def test_last_years_course_left_as_enrolled_does_not_come_back():
    """去年の「履修中」が残っていても、年度が違えば今年の授業に出さない（1〜3月は前の年度）。"""
    from datetime import date

    rows = [{"id": key, "url": "", "properties": {
        "科目名": _title(name), "曜日": _select("月"), "時限": {"number": 2},
        "状態": _select("履修中"), "学期": _select("秋学期"), "年度": {"number": year}}}
        for key, name, year in (("old", "去年のデータベース", 2025), ("now", "データベース", 2026))]

    course = notion_sync.CourseNotion(FakeNotion(courses=rows), STATE)
    assert [c["subject"] for c in course.courses_on("月", date(2026, 9, 21))] == ["データベース"]
    assert [c["subject"] for c in course.courses_on("月", date(2027, 1, 18))] == ["データベース"]


@pytest.mark.parametrize(("term", "spring", "autumn"), [
    ("春ク", True, False), ("夏ク", True, False), ("秋ク", False, True), ("冬ク", False, True),
    ("その他", True, True), ("", True, True), ("通年", True, True),
])
def test_quarters_follow_their_semester(term, spring, autumn):
    from datetime import date

    from kei_agent_course import periods

    assert periods.in_term(term, date(2026, 5, 11)) is spring
    assert periods.in_term(term, date(2026, 11, 9)) is autumn


def test_academic_year_starts_in_april():
    from datetime import date

    from kei_agent_course import periods

    assert periods.academic_year(date(2027, 3, 31)) == 2026
    assert periods.academic_year(date(2027, 4, 1)) == 2027


def test_next_weekday_is_today_or_later():
    from datetime import date

    from kei_agent_course import periods

    friday = date(2026, 9, 25)
    assert periods.next_weekday("金", friday) == friday
    assert periods.next_weekday("月", friday) == date(2026, 9, 28)
    assert periods.next_weekday("他", friday) == friday


def test_waseda_periods_turn_into_times():
    from datetime import date

    from kei_agent_course import periods

    assert weekday(date(2026, 9, 21)) == "月"
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


def test_course_reads_box_and_fully_manages_the_course_notion_through_the_gateway(config):
    """Box は読むだけ。Notion はゲートウェイ（授業ホームの中）で全部でき、アカウントの Notion 連携は使わない。"""
    from kei_agent import guard, themes
    from kei_agent.agent_policy import policy_of

    ws = themes.agent_workspace(config, "course")
    permissions = guard.claude_permissions(config, ws, policy_of("course"))
    box = [n for n in permissions["allow"] if n.startswith("mcp__claude_ai_Box__")]
    assert box and not [n for n in box if any(w in n for w in ("upload", "create", "update", "move", "copy", "set_"))]
    # 「提出済みにして」も「このページを移して」も頼める（ゲートウェイの道具を全部）
    assert "mcp__kei-notion" in permissions["allow"]
    assert "mcp__claude_ai_Notion" in permissions["deny"]


def test_course_notion_goes_through_the_gateway_as_course(monkeypatch):
    """授業の Notion は、ゲートウェイの course（授業ホームだけに届く）の合言葉で呼ぶ。"""
    from kei_agent.notion import gateway_client_token

    monkeypatch.setenv("KEI_AGENT_NOTION_GATEWAY_TOKEN", "master")
    client = notion_sync._client(STATE)
    assert client.notion.base_url.endswith("/notion/v1")
    assert client.notion.token == gateway_client_token("master", "course")


def test_course_notion_without_the_gateway_password_is_a_notion_error():
    from kei_agent.notion import NotionError

    with pytest.raises(NotionError, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        notion_sync.courses_on("月", state=STATE)
