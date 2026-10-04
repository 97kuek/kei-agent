
import pytest

from kei_agent_modules.course import academic_sync, school
from kei_agent_modules.course.academic_record import AcademicRecord, Grade, Requirement
from kei_agent_modules.course.academic_sync import AcademicSync, grade_key
from kei_agent_modules.course.notion_props import plain

WASEDA = school.load({"school": "waseda"})


class RowsNotion:
    """表ごとの行を持ち、読んだ回数と書き込みを数える Notion の fake。"""

    def __init__(self, rows=None, *, writable=True):
        self.rows = {key: [] for key in ("courses", "grades", "requirements")} | (rows or {})
        self.reads: list[str] = []
        self.writes: list[tuple] = []
        self.writable = writable

    def paginate(self, _method, path, _body=None):
        key = path.split("/")[2]
        self.reads.append(key)
        return self.rows[key]

    def request(self, method, path, body=None):
        if not self.writable:
            raise AssertionError((method, path, body))
        self.writes.append((method, path, body))
        if method == "POST" and path == "/pages":
            source = body["parent"]["data_source_id"]
            row = {"id": f"{source}-{len(self.rows[source])}", "properties": body["properties"]}
            self.rows[source].append(row)
            return row
        page_id = path.rsplit("/", 1)[-1]
        row = next(row for rows in self.rows.values() for row in rows if row["id"] == page_id)
        row["properties"].update(body["properties"])
        return row


STATE = {"databases": {key: {"data_source_id": key} for key in ("courses", "grades", "requirements")}}
MATH = Grade("数学", 2025, "春期", 2, "A", 4, "基礎")


def course(page_id, name="数学", year=2025, term="春学期"):
    return {"id": page_id, "properties": {"Name": {"title": [{"plain_text": name}]}, "Year": {"number": year},
                                          "Term": {"select": {"name": term}}}}


def record(grades=(), requirements=()):
    return AcademicRecord(grades=tuple(grades), requirements=tuple(requirements))


def test_grades_and_requirements_use_the_new_columns_and_the_course_is_left_alone():
    notion = RowsNotion({"courses": [course("course-1")]})
    AcademicSync(notion, STATE, WASEDA).sync(record(
        [Grade("数学", 2025, "春期", 2, "A", 4, "Ｂ群 / 数学")], [Requirement("総合計", "", 124, 10, 114, "Total")]))

    grade = notion.rows["grades"][0]["properties"]
    assert set(grade) == {"Name", "Grade", "GP", "Credits", "Category", "Group", "Record ID", "Course"}
    assert (plain(grade["Group"]), plain(grade["Category"]), plain(grade["Grade"])) == ("Ｂ群", "数学", "A")
    assert grade["Course"] == {"relation": [{"id": "course-1"}]}
    requirement = notion.rows["requirements"][0]["properties"]
    assert set(requirement) == {"Name", "Remaining", "Required", "Counted", "Group", "Kind", "Record ID"}
    assert (requirement["Remaining"], requirement["Counted"]) == ({"number": 114}, {"number": 10})
    # 科目区分・科目群は成績にだけ書き、授業には書かない
    assert notion.rows["courses"] == [course("course-1")]


def test_a_requirement_kind_outside_the_options_is_written_as_other():
    notion = RowsNotion()
    AcademicSync(notion, STATE, WASEDA).sync(record(requirements=[
        Requirement("総合計", "", 124, 10, 114, "Total"), Requirement("自由科目", "", 0, 2, 0, "区分")]))

    assert [row["properties"]["Kind"] for row in notion.rows["requirements"]] == [
        {"select": {"name": "Total"}}, {"select": {"name": "Other"}}]


def test_a_grade_without_its_course_creates_a_done_course_once():
    """「授業」に無い科目の成績は、終わった授業を1度だけ作って結ぶ。取り込み直しても2行にしない。"""
    notion = RowsNotion()
    sync = AcademicSync(notion, STATE, WASEDA)

    first = sync.sync(record([MATH]))
    second = sync.sync(record([MATH]))

    new_course, = notion.rows["courses"]
    assert new_course["properties"]["Status"] == {"select": {"name": "Done"}}
    assert (new_course["properties"]["Year"], new_course["properties"]["Term"]) == (
        {"number": 2025}, {"select": {"name": "春学期"}})
    assert "Category" not in new_course["properties"] and "Group" not in new_course["properties"]
    assert first.new_courses == ("数学 / 2025 / 春学期",) and second.new_courses == ()
    grade, = notion.rows["grades"]
    assert grade["properties"]["Course"] == {"relation": [{"id": new_course["id"]}]}
    assert second.unchanged == {"grades": 1, "requirements": 0}


def test_grade_identity_survives_a_corrected_grade_or_credit_value():
    before = Grade("数学", 2025, "春期", 2, "B", 2, "Ｂ群 / 数学")
    corrected = Grade("数学", 2025, "春期", 3, "A", 4, "Ｂ群 / 数学")

    assert grade_key(before) == grade_key(corrected) == "grade:2025:春期:数学"


def test_academic_sync_upserts_same_record_without_duplicate_pages():
    """2回目の同期は、同じ行を作らず、変わった要件だけを決まった鍵で書き直す。"""
    notion = RowsNotion()
    sync = AcademicSync(notion, STATE, WASEDA)
    first = sync.sync(record([MATH], [Requirement("総合計", "", 124, 10, 114, "Total")]))
    second = sync.sync(record([MATH], [Requirement("総合計", "", 124, 10, 114, "Total")]))
    assert first.created == {"grades": 1, "requirements": 1}
    assert second.created == {"grades": 0, "requirements": 0}

    updated = sync.sync(record(requirements=[Requirement("総合計", "", 124, 12, 112, "Total")]))
    assert updated.updated == {"grades": 0, "requirements": 1}
    assert updated.created == {"grades": 0, "requirements": 0}


def test_ambiguous_course_is_reported_without_grade_relation():
    notion = RowsNotion({"courses": [course("math-a"), course("math-b")]})
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert result.ambiguous_relations == ("成績: 数学 / 2025 / 春期",)
    assert "Course" not in notion.rows["grades"][0]["properties"]
    assert len(notion.rows["courses"]) == 2


def test_academic_sync_preserves_an_existing_grade_course_relation():
    notion = RowsNotion({
        "courses": [course("matched-course")],
        "grades": [{"id": "grade-1", "properties": {
            "Name": {"title": [{"plain_text": "数学"}]},
            "Record ID": {"rich_text": [{"plain_text": "grade:2025:春期:数学"}]},
            "Credits": {"number": 2}, "Grade": {"rich_text": [{"plain_text": "A"}]},
            "GP": {"number": 4}, "Group": {"rich_text": [{"plain_text": "基礎"}]},
            "Category": {"rich_text": []},
            "Course": {"relation": [{"id": "manually-linked-course"}]},
        }}],
    }, writable=False)
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert result.unchanged == {"grades": 1, "requirements": 0}


def test_a_linked_grade_with_no_name_match_creates_no_course():
    """手で授業に結んだ成績は、名前が合う授業が無くても、新しい授業の行を作らない（成績の値だけ直す）。"""
    notion = RowsNotion({
        "grades": [{"id": "grade-1", "properties": {
            "Name": {"title": [{"plain_text": "数学"}]},
            "Record ID": {"rich_text": [{"plain_text": "grade:2025:春期:数学"}]},
            "Credits": {"number": 2}, "Grade": {"rich_text": [{"plain_text": "B"}]},
            "GP": {"number": 3}, "Group": {"rich_text": [{"plain_text": "基礎"}]},
            "Category": {"rich_text": []},
            "Course": {"relation": [{"id": "manually-linked-course"}]},
        }}],
    })
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert notion.rows["courses"] == [] and result.new_courses == ()
    grade, = notion.rows["grades"]
    assert grade["properties"]["Course"] == {"relation": [{"id": "manually-linked-course"}]}
    assert grade["properties"]["Grade"] != {"rich_text": [{"plain_text": "B"}]}


class _ReadingSchool:
    """成績のファイルを読んだことにする学校（読んだファイルを覚える）。"""

    def __init__(self, record):
        self.record = record
        self.files = []

    def read_record(self, files):
        self.files.append(files)
        return self.record


def _school_from_config(monkeypatch, record):
    part = _ReadingSchool(record)
    monkeypatch.setattr(academic_sync, "load_config", lambda: object())
    monkeypatch.setattr(academic_sync, "from_config", lambda _config: part)
    return part


def test_academic_import_cli_dry_run_never_reads_state_or_writes(tmp_path, monkeypatch):
    record = AcademicRecord(grades=(), requirements=())
    part = _school_from_config(monkeypatch, record)
    monkeypatch.setattr(academic_sync, "read_state", lambda: (_ for _ in ()).throw(AssertionError("state")))

    assert academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html"), "--dry-run"]) == 0
    assert academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html")]) == 2
    # ファイルの読み方は学校の部品が決める（渡した順のまま）
    assert part.files[0] == [tmp_path / "grades.html", tmp_path / "credits.html"]


def test_academic_import_cli_says_why_the_files_cannot_be_read(tmp_path, monkeypatch):
    """学校の部品が選ばれていない、ファイルが違う、などは、トレースバックではなく理由で止まる。"""
    monkeypatch.setattr(academic_sync, "load_config", lambda: object())
    monkeypatch.setattr(academic_sync, "from_config", lambda _config: school.School())
    with pytest.raises(SystemExit, match="学校の部品が選ばれていません"):
        academic_sync.main([str(tmp_path / "grades.html"), "--dry-run"])
    monkeypatch.setattr(academic_sync, "from_config", lambda _config: WASEDA)
    with pytest.raises(SystemExit, match="成績を読めません"):
        academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html"), "--dry-run"])


def test_academic_import_cli_writes_only_through_the_gateway(tmp_path, monkeypatch):
    """書き込みはゲートウェイの course（授業ホームだけに届く）として。合言葉が無ければ state も読まずに止まる。"""
    record = AcademicRecord(grades=(), requirements=())
    _school_from_config(monkeypatch, record)
    monkeypatch.setattr(academic_sync, "read_state", lambda: (_ for _ in ()).throw(AssertionError("state")))

    with pytest.raises(SystemExit, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html"), "--apply"])


@pytest.mark.parametrize("grades, in_notion", [
    ([MATH, Grade("数学", 2025, "春期", 2, "B", 3, "基礎")], []),          # 読んだ成績の中で重なる
    ([MATH], ["a", "b"]),                                                  # Notion にもう2行ある
])
def test_duplicate_record_keys_stop_before_any_write(grades, in_notion):
    row = {"properties": {"Record ID": {"rich_text": [{"plain_text": "grade:2025:春期:数学"}]}}}
    notion = RowsNotion({"grades": [{"id": page_id, **row} for page_id in in_notion]})

    with pytest.raises(ValueError, match="重複"):
        AcademicSync(notion, STATE, WASEDA).sync(record(grades))

    assert notion.writes == []


def test_each_database_is_read_once_per_run():
    notion = RowsNotion()
    AcademicSync(notion, STATE, WASEDA).sync(record(
        [Grade(f"科目{i}", 2025, "春期", 2, "A", 4, "基礎") for i in range(5)],
        [Requirement(f"要件{i}", "", 2, 2, 0, "Category") for i in range(3)],
    ))

    assert sorted(notion.reads) == ["courses", "grades", "requirements"]


def test_grade_matches_a_course_whose_name_differs_only_in_width():
    """全角・半角の違いだけの科目名は、同じ科目として結ぶ（授業を作らない）。"""
    notion = RowsNotion({"courses": [course("course-b", "情報セキュリティB", term="秋学期")]})

    AcademicSync(notion, STATE, WASEDA).sync(record([Grade("情報セキュリティＢ", 2025, "秋期", 2, "A", 4, "基礎")]))

    (_method, _path, body), = notion.writes
    assert body["properties"]["Course"] == {"relation": [{"id": "course-b"}]}
