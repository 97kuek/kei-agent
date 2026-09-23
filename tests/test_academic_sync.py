import pytest

from kei_agent_course import academic_sync
from kei_agent_course.academic_record import AcademicRecord, GPAEntry, Grade, Requirement
from kei_agent_course.academic_sync import AcademicSync, inspect_course_home


def test_inspect_reports_duplicates_without_mutating_notion():
    class Notion:
        writes = []

        def children(self, _page_id):
            return [
                {"type": "child_database", "child_database": {"title": "授業"}},
                {"type": "child_database", "child_database": {"title": "授業"}},
                {"type": "child_database", "child_database": {"title": "雑記"}},
            ]

    notion = Notion()
    report = inspect_course_home(notion, "home", {"databases": {}})

    assert report.duplicates == ("授業",)
    assert report.unmanaged == ("雑記",)
    assert notion.writes == []


def test_setup_refuses_to_choose_between_duplicate_canonical_databases(tmp_path):
    from kei_agent.notion import NotionError
    from kei_agent_course.notion_setup import CourseSetup

    class Notion:
        def children(self, _page_id):
            return [
                {"id": "course-a", "type": "child_database", "child_database": {"title": "授業"}},
                {"id": "course-b", "type": "child_database", "child_database": {"title": "授業"}},
            ]

    setup = CourseSetup(Notion(), "home", tmp_path / "notion-course.json")
    with pytest.raises(NotionError, match="重複"):
        setup.run()


def test_academic_sync_upserts_same_record_without_duplicate_pages():
    class Notion:
        def __init__(self):
            self.rows = {"courses": [], "grades": [], "requirements": [], "gpa": []}

        def paginate(self, _method, path, _body=None):
            return self.rows[path.split("/")[2]]

        def request(self, method, path, body=None):
            if method == "POST" and path == "/pages":
                data_source = body["parent"]["data_source_id"]
                row = {"id": f"{data_source}-{len(self.rows[data_source])}", "properties": body["properties"]}
                self.rows[data_source].append(row)
                return row
            raise AssertionError((method, path, body))

    state = {"databases": {key: {"data_source_id": key} for key in ("courses", "grades", "requirements", "gpa")}}
    record = AcademicRecord(
        grades=(Grade("数学", 2025, "春期", 2, "A", 4, "基礎"),),
        requirements=(Requirement("総合計", "", 124, 10, 10, 114, "総合計"),),
        gpa=(GPAEntry("2025年度（春学期）", 2025, 4, "春学期"),),
    )
    sync = AcademicSync(Notion(), state)

    first = sync.sync(record)
    second = sync.sync(record)

    assert first.created == {"grades": 1, "requirements": 1, "gpa": 1}
    assert second.created == {"grades": 0, "requirements": 0, "gpa": 0}


def test_ambiguous_course_is_reported_without_grade_relation():
    class Notion:
        def __init__(self):
            self.rows = {"courses": [
                {"id": "math-a", "properties": {"科目名": {"title": [{"plain_text": "数学"}]},
                                                    "年度": {"number": 2025}, "学期": {"select": {"name": "春学期"}}}},
                {"id": "math-b", "properties": {"科目名": {"title": [{"plain_text": "数学"}]},
                                                    "年度": {"number": 2025}, "学期": {"select": {"name": "春学期"}}}},
            ], "grades": [], "requirements": [], "gpa": []}

        def paginate(self, _method, path, _body=None):
            return self.rows[path.split("/")[2]]

        def request(self, _method, _path, body=None):
            source = body["parent"]["data_source_id"]
            row = {"id": "grade-1", "properties": body["properties"]}
            self.rows[source].append(row)
            return row

    state = {"databases": {key: {"data_source_id": key} for key in ("courses", "grades", "requirements", "gpa")}}
    result = AcademicSync(Notion(), state).sync(AcademicRecord(
        grades=(Grade("数学", 2025, "春期", 2, "A", 4, "基礎"),), requirements=(), gpa=(),
    ))

    assert result.ambiguous_relations == ("成績履歴: 数学 / 2025 / 春期",)


def test_academic_sync_updates_an_existing_requirement_by_stable_key():
    class Notion:
        def __init__(self):
            self.rows = {"courses": [], "grades": [], "requirements": [{
                "id": "requirement-1", "properties": {
                    "Kei Agent 要件ID": {"rich_text": [{"plain_text": "requirement::総合計"}]},
                    "所定": {"number": 124}, "既得": {"number": 10}, "算入": {"number": 10}, "残り": {"number": 114},
                },
            }], "gpa": []}

        def paginate(self, _method, path, _body=None):
            return self.rows[path.split("/")[2]]

        def request(self, method, path, body=None):
            if method == "PATCH" and path == "/pages/requirement-1":
                self.rows["requirements"][0]["properties"].update(body["properties"])
                return self.rows["requirements"][0]
            raise AssertionError((method, path, body))

    state = {"databases": {key: {"data_source_id": key} for key in ("courses", "grades", "requirements", "gpa")}}
    result = AcademicSync(Notion(), state).sync(AcademicRecord(
        grades=(), requirements=(Requirement("総合計", "", 124, 12, 12, 112, "総合計"),), gpa=(),
    ))

    assert result.updated == {"grades": 0, "requirements": 1, "gpa": 0}


def test_academic_import_cli_dry_run_never_reads_state_or_writes(tmp_path, monkeypatch):
    record = AcademicRecord(grades=(), requirements=(), gpa=())
    monkeypatch.setattr(academic_sync, "parse_academic_record", lambda *_paths: record)
    monkeypatch.setattr(academic_sync, "read_state", lambda: (_ for _ in ()).throw(AssertionError("state")))

    assert academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html"), "--dry-run"]) == 0
    assert academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html")]) == 2
