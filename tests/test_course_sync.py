"""Moodle の締切を Notion の「課題」に取り込むところ。"""

import json
from datetime import date, datetime

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は course のグループに入っている（uv run --group course）")

from kei_agent.conversation.dates import weekday
from kei_agent_modules.course import notion_sync, school
from kei_agent_modules.course.ics import Event

# 時限の時刻と学期は、早稲田の部品の既定
WASEDA = school.load({"school": "waseda"})

STATE = {"home_page_id": "course-home", "databases": {
    "courses": {"data_source_id": "ds-courses"},
    "assignments": {"data_source_id": "ds-assignments"},
    "study_logs": {"data_source_id": "ds-study-logs"},
}}

REPORT = Event(uid="2345678@moodle", summary="第3回レポート の 提出期限",
               starts_at=datetime(2026, 9, 25, 23, 59), course="データベース(2019ZZ26)",
               url="https://wsdmoodle.waseda.jp/mod/assign/view.php?id=12345")

QUIZ = Event(uid="q1@moodle", summary="Short test 1 (14:20-14:50) の受験可能期間の終了",
             starts_at=datetime(2026, 10, 2, 14, 50), course="マルチメディア工学Ｂ(2019ZZ1B)")


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


def test_calendar_assignment_snapshot_reads_all_rows_with_course_link_and_clean_title():
    """手入力の行も含めて全部読み（書き込みはしない）、締切のまとめ知らせ用に科目名・Moodle のリンク・言い回しを落とした課題名を返す。"""
    assignments = [_row(REPORT, page_id=f"p-{i}") for i in range(21)]
    assignments.append({"id": "manual", "url": "https://notion.example/manual", "properties": {
        "課題": _title("手入力の課題"), "締切": {"date": {"start": "2026-10-01T23:59:00+09:00"}},
        "状態": {"status": {"name": "未着手"}},
    }})
    notion = FakeNotion(courses=COURSE_ROWS, assignments=assignments)

    snapshot = notion_sync.CourseNotion(notion, STATE).calendar_assignments(
        days=30, today=date(2026, 9, 24))

    assert snapshot["complete"] is True
    assert len(snapshot["items"]) == 22
    items = {item["id"]: item for item in snapshot["items"]}
    assert items["manual"] == {
        "id": "manual", "title": "手入力の課題", "due": "2026-10-01T23:59:00+09:00",
        "status": "未着手", "url": "https://notion.example/manual",
        "course": "", "moodle": "", "moodle_id": ""}
    assert (items["p-0"]["title"], items["p-0"]["course"], items["p-0"]["moodle"], items["p-0"]["moodle_id"]) == (
        "第3回レポート", "データベース", REPORT.url, REPORT.uid)
    assert notion.calls == []


def test_a_new_deadline_becomes_a_row_with_sections():
    """新しい締切は行になり、本文に見出しの型を入れる。すでに本文のあるページには足さない。"""
    notion = FakeNotion(courses=COURSE_ROWS, page_children={"new-row": []})
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
    # 通知の行は Moodle の言い回しを落とし、締切の時刻を付ける
    assert result.added == ["`09/25 23:59` データベース / 第3回レポート"] and result.unchanged == 0
    assert notion.appended_children("new-row") == ["やること", "提出物", "進捗メモ", "資料・リンク"]

    notion.page_children["existing-row"] = [{"type": "paragraph"}]
    notion_sync.CourseNotion(notion, STATE).ensure_assignment_template("existing-row")
    assert notion.appended_children("existing-row") == []


def test_the_notification_label_moves_the_time_range():
    """末尾の時刻幅（14:20-14:50）は日付側の囲みに寄せる。"""
    notion = FakeNotion(courses=[*COURSE_ROWS, {"id": "page-mm", "properties": {"科目名": _title("マルチメディア工学Ｂ")}}])
    result = _sync(notion, [QUIZ])

    assert result.added == ["`10/02 14:20-14:50` マルチメディア工学Ｂ / Short test 1"]


def test_assignment_title_removes_only_quoted_submission_suffix():
    assert notion_sync.assignment_title("「Assignment A」の提出期限") == "Assignment A"
    assert notion_sync.assignment_title("【ミニテスト】PC の受験可能期間の終了") == "【ミニテスト】PC の受験可能期間の終了"
    assert notion_sync.assignment_title("アンケート終了 ") == "アンケート終了 "
    assert notion_sync.assignment_title(" 「Assignment A」の提出期限") == " 「Assignment A」の提出期限"


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


def test_a_course_that_is_not_taken_is_skipped_unless_all_are_asked():
    """Moodle のカレンダーには履修していない科目も並ぶので、既定では入れずに名前だけ知らせる。
    全部入れると言われたときは、科目の欄を空にして入れる。"""
    notion = FakeNotion(courses=COURSE_ROWS)
    result = _sync(notion, [OTHER])
    assert notion.calls == []
    assert result.other_courses == ["新入生セミナー"] and not result.added
    assert "履修していない科目" in result.summary()

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


@pytest.mark.parametrize("state", [None, {"databases": {key: {} for key in ("courses", "assignments", "study_logs")}}])
def test_missing_or_legacy_state_says_what_to_run(tmp_path, state):
    """控えが無い・古い3つの DB の形なら、setup を案内する。"""
    path = tmp_path / "notion-course.json"
    if state is not None:
        path.write_text(json.dumps(state))
    with pytest.raises(notion_sync.SyncError, match="kei-agent-module course setup"):
        notion_sync.read_state(path)


# 履修中の科目（朝のまとめで、時限を時刻に直すのに使う）


def _select(name):
    return {"select": {"name": name}}


def _course(key, name, day="月", period=2, status="履修中", term="秋学期", year=None):
    props = {"科目名": _title(name), "曜日": _select(day), "時限": {"number": period}, "状態": _select(status)}
    if term:
        props["学期"] = _select(term)
    if year:
        props["年度"] = {"number": year}
    return {"id": key, "url": f"https://notion/{key}", "properties": props}


COURSE_ROWS_FULL = [
    _course("p1", "データベース"),
    _course("p2", "次世代ネットワーク", day="金", period=4),
    _course("p3", "去年の科目", period=1, status="終了", term=None),
    _course("p4", "プロジェクト研究B", day="他", period=None),
]


def _courses(rows):
    return notion_sync.CourseNotion(FakeNotion(courses=rows), STATE, WASEDA)


def _subjects(found):
    return [c["subject"] for c in found]


def test_courses_on_and_current_courses_take_only_enrolled_courses_by_period():
    """終了した科目は出さない。曜日を省くと全部を時限の早い順に（時限のない集中講義などは最後）。"""
    course = _courses(COURSE_ROWS_FULL)
    monday = date(2026, 9, 21)
    assert _subjects(course.courses_on("月", monday)) == ["データベース"]
    assert _subjects(course.courses_on(on=monday)) == ["データベース", "次世代ネットワーク", "プロジェクト研究B"]
    assert _subjects(course.current_courses(monday)) == ["データベース", "次世代ネットワーク", "プロジェクト研究B"]


def test_last_years_course_left_as_enrolled_does_not_come_back():
    """去年の「履修中」が残っていても、年度が違えば今年の授業に出さない（1〜3月は前の年度）。"""
    course = _courses([_course("old", "去年のデータベース", year=2025), _course("now", "データベース", year=2026)])
    assert _subjects(course.courses_on("月", date(2026, 9, 21))) == ["データベース"]
    assert _subjects(course.courses_on("月", date(2027, 1, 18))) == ["データベース"]


def test_courses_on_drops_the_other_term_but_keeps_one_without_a_term():
    """学期の終わった科目が「履修中」で残っていても、今の学期のものだけを返す。
    学期が空の科目は、隠すより出す（見落としのほうが困る）。"""
    course = _courses([*COURSE_ROWS_FULL, _course("p5", "春の科目", period=3, term="春学期"),
                       _course("p6", "通年の科目", period=5, term="通年"),
                       _course("p9", "学期なし", period=6, term=None)])
    assert _subjects(course.courses_on("月", date(2026, 9, 21))) == ["データベース", "通年の科目", "学期なし"]
    assert _subjects(course.courses_on("月", date(2026, 5, 11))) == ["春の科目", "通年の科目", "学期なし"]


@pytest.mark.parametrize(("term", "spring", "autumn"), [
    ("春ク", True, False), ("夏ク", True, False), ("秋ク", False, True), ("冬ク", False, True),
    ("その他", True, True), ("", True, True), ("通年", True, True),
])
def test_quarters_follow_their_semester(term, spring, autumn):
    assert WASEDA.in_term(term, date(2026, 5, 11)) is spring
    assert WASEDA.in_term(term, date(2026, 11, 9)) is autumn


def test_waseda_calendar_years_weekdays_and_periods():
    """年度は4月始まり。次の曜日は今日を含む。時限は時刻に直し、時限なし・無い時限は None。"""
    assert WASEDA.academic_year(date(2027, 3, 31)) == 2026
    assert WASEDA.academic_year(date(2027, 4, 1)) == 2027
    friday = date(2026, 9, 25)
    assert school.next_weekday("金", friday) == friday
    assert school.next_weekday("月", friday) == date(2026, 9, 28)
    assert school.next_weekday("他", friday) == friday
    assert weekday(date(2026, 9, 21)) == "月"
    start, end = WASEDA.at(date(2026, 9, 21), 2)
    assert (start.hour, start.minute) == (10, 40) and (end.hour, end.minute) == (12, 20)
    assert WASEDA.at(date(2026, 9, 21), None) is None      # 時限なし（集中講義）
    assert WASEDA.at(date(2026, 9, 21), 9) is None         # 無い時限


def test_course_reads_box_and_fully_manages_the_course_notion_through_the_gateway(config):
    """Box は読むだけ。Notion はゲートウェイ（授業ホームの中）で全部でき、アカウントの Notion 連携は使わない。"""
    from kei_agent.execution import guard
    from kei_agent.execution.agent_policy import policy_of
    from kei_agent.workspaces import themes

    ws = themes.agent_workspace(config, "course")
    permissions = guard.claude_permissions(config, ws, policy_of("course"))
    box = [n for n in permissions["allow"] if n.startswith("mcp__claude_ai_Box__")]
    assert box and not [n for n in box if any(w in n for w in ("upload", "create", "update", "move", "copy", "set_"))]
    # 「提出済みにして」も「このページを移して」も頼める（ゲートウェイの道具を全部）
    assert "mcp__kei-notion" in permissions["allow"]
    assert "mcp__claude_ai_Notion" in permissions["deny"]


def test_course_notion_goes_through_the_gateway_as_course(monkeypatch):
    """授業の Notion は、ゲートウェイの course（授業ホームだけに届く）の合言葉で呼ぶ。"""
    import os
    from pathlib import Path

    from fakes import write_config

    from kei_agent.storage.notion import gateway_client_token
    write_config(Path(os.environ["KEI_AGENT_HOME"]) / "config.toml", '[notion]\ncourse_home = "abc"\n')
    monkeypatch.setenv("KEI_AGENT_NOTION_GATEWAY_TOKEN", "master")
    client = notion_sync._client(STATE)
    assert client.notion.base_url.endswith("/notion/v1")
    assert client.notion.token == gateway_client_token("master", "course")


def test_course_notion_without_the_gateway_password_is_a_notion_error():
    from kei_agent.storage.notion import NotionError

    with pytest.raises(NotionError, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        notion_sync.courses_on("月", state=STATE)
