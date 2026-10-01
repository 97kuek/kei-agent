import copy

import pytest

from kei_agent.storage.notion import NotionError
from kei_agent_modules.course import academic_sync, school
from kei_agent_modules.course.academic_record import AcademicRecord, GPAEntry, Grade, Requirement
from kei_agent_modules.course.academic_sync import AcademicSync, grade_key
from kei_agent_modules.course.notion_setup import CourseSetup

WASEDA = school.load({"school": "waseda"})


def _property(property_id: str, kind: str = "rich_text") -> dict:
    return {"id": property_id, "type": kind, kind: {}}


class ExistingCourseHomeNotion:
    """実在の授業ホームと同じ title / relation 名を返す Notion 境界の fake。"""

    def __init__(self, assignment_title: str = "タイトル", duplicate_title: str = ""):
        names = {
            "courses": {
                "科目名": "title", "科目コード": "rich_text", "年度": "number", "学期": "select",
                "曜日": "select", "時限": "number", "Moodle": "url", "状態": "select",
                "課題": "relation", "学習ログ": "relation", "成績履歴": "relation",
                "単位要件": "relation", "GPA推移": "relation",
            },
            "assignments": {
                assignment_title: "title", "締切": "date", "状態": "status", "Moodle": "url",
                "見積時間": "number", "実績時間": "number", "出どころ": "select", "Moodle ID": "rich_text",
                "最終同期": "last_edited_time", "科目": "relation",
            },
            "study_logs": {
                "タイトル": "title", "Kei Agent 記録ID": "rich_text", "日付": "date", "時間（分）": "number",
                "メモ": "rich_text", "Slack": "url", "科目": "relation",
            },
            "grades": {
                "科目名": "title", "取得年度": "number", "学期": "select", "単位": "number", "成績": "rich_text",
                "GP": "number", "科目区分": "rich_text", "取り込み元": "rich_text", "授業": "relation",
            },
            "requirements": {
                "要件名": "title", "所定単位": "number", "既得単位": "number", "算入単位": "number",
                "残り単位": "number", "取り込み元": "rich_text", "大区分": "rich_text", "集計種別": "select",
                "対象授業": "relation",
            },
            "gpa": {
                "期間": "title", "年度": "number", "種別": "select", "GPA": "number", "対象授業": "relation",
            },
        }
        self.data_sources = {
            key: {"properties": {name: _property(f"{key}:{name}", kind) for name, kind in props.items()}}
            for key, props in names.items()
        }
        self.calls: list[tuple[str, str, dict | None]] = []
        self.deleted: list[str] = []
        self.duplicate_title = duplicate_title

    def children(self, _page_id):
        blocks = [
            {"id": key, "type": "child_database", "child_database": {"title": title}}
            for key, title in (("courses", "授業"), ("assignments", "課題"), ("study_logs", "学習ログ"),
                               ("grades", "📊 成績履歴"), ("requirements", "🎓 単位要件"), ("gpa", "📈 GPA推移"))
        ]
        if self.duplicate_title:
            blocks.append({"id": "duplicate", "type": "child_database", "child_database": {"title": self.duplicate_title}})
        return blocks

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path.startswith("/databases/"):
            database_id = path.rsplit("/", 1)[-1]
            return {"id": database_id, "data_sources": [{"id": database_id}], "url": f"https://notion.so/{database_id}"}
        if method == "GET" and path.startswith("/data_sources/"):
            return copy.deepcopy(self.data_sources[path.rsplit("/", 1)[-1]])
        if method == "PATCH" and path.startswith("/data_sources/"):
            key = path.rsplit("/", 1)[-1]
            properties = self.data_sources[key]["properties"]
            for name, definition in (body or {}).get("properties", {}).items():
                if name in properties:
                    properties[name].update(definition)
                    continue
                if name.startswith(f"{key}:"):
                    old_name = next(old for old, prop in properties.items() if prop["id"] == name)
                    new_name = definition["name"]
                    properties[new_name] = properties.pop(old_name) | {"name": new_name}
                    continue
                kind = next(iter(definition))
                properties[name] = _property(f"{key}:{name}", kind)
            return copy.deepcopy(self.data_sources[key])
        raise AssertionError((method, path, body))

    def property_renamed(self, key: str, old: str, new: str) -> bool:
        return old not in self.data_sources[key]["properties"] and new in self.data_sources[key]["properties"]

    def title_property_count(self, key: str) -> int:
        return sum(prop["type"] == "title" for prop in self.data_sources[key]["properties"].values())

    def added_relation_names(self) -> set[str]:
        return {
            name for source in self.data_sources.values() for name, prop in source["properties"].items()
            if prop["type"] == "relation" and name in {"算入成績", "対象成績"}
        }


def test_setup_fits_the_existing_home_without_a_second_title_or_lost_rows(tmp_path):
    """実在の授業ホームに合わせて、題の列は名前を変えて使い、足りない列と関連だけを足す（消さない）。"""
    notion = ExistingCourseHomeNotion()
    CourseSetup(notion, "home", tmp_path / "notion-course.json").run()

    patched_names = {
        name for method, path, body in notion.calls if method == "PATCH" and path == "/data_sources/grades"
        for name in (body or {}).get("properties", {})
    }
    assert "タイトル" not in patched_names
    grades = notion.data_sources["grades"]["properties"]
    assert "Kei Agent 成績ID" in grades and "科目群" in grades
    assert notion.property_renamed("assignments", "タイトル", "課題")
    assert notion.property_renamed("grades", "科目名", "授業名")
    assert notion.title_property_count("assignments") == notion.title_property_count("grades") == 1
    courses = notion.data_sources["courses"]["properties"]
    assert courses["科目群"]["type"] == "select"
    assert courses["必選区分"]["type"] == "select"
    assert courses["単位"]["type"] == "number"
    assert {"算入成績", "対象成績"} <= notion.added_relation_names()
    assert notion.deleted == []


@pytest.mark.parametrize("duplicate", ["授業", "📊 成績履歴"])
def test_setup_checks_all_canonical_duplicates_before_any_write(tmp_path, duplicate):
    """同じ名前の表が2つあれば、どちらかを選ばずに、何も書く前に止まる。"""
    notion = ExistingCourseHomeNotion(duplicate_title=duplicate)
    setup = CourseSetup(notion, "home", tmp_path / "notion-course.json")

    with pytest.raises(NotionError, match="重複"):
        setup.run()

    assert notion.calls == []
    assert not setup.state_path.exists()


class RowsNotion:
    """表ごとの行を持ち、読んだ回数と書き込みを数える Notion の fake。"""

    def __init__(self, rows=None, *, writable=True):
        self.rows = {key: [] for key in ("courses", "grades", "requirements", "gpa")} | (rows or {})
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


STATE = {"databases": {key: {"data_source_id": key} for key in ("courses", "grades", "requirements", "gpa")}}
MATH = Grade("数学", 2025, "春期", 2, "A", 4, "基礎")


def course(page_id, name="数学", year=2025, term="春学期"):
    return {"id": page_id, "properties": {"科目名": {"title": [{"plain_text": name}]}, "年度": {"number": year},
                                          "学期": {"select": {"name": term}}}}


def record(grades=(), requirements=(), gpa=()):
    return AcademicRecord(grades=tuple(grades), requirements=tuple(requirements), gpa=tuple(gpa))


def test_academic_sync_writes_existing_property_names():
    notion = RowsNotion({"courses": [course("course-1")]})
    AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    properties = notion.rows["grades"][0]["properties"]
    assert set(properties) >= {"授業名", "科目群", "Kei Agent 成績ID", "授業"}
    assert properties["科目群"]["rich_text"][0]["text"]["content"] == "基礎"
    assert "タイトル" not in properties
    assert properties["授業"] == {"relation": [{"id": "course-1"}]}


def test_grade_identity_survives_a_corrected_grade_or_credit_value():
    before = Grade("数学", 2025, "春期", 2, "B", 2, "Ｂ群 / 数学")
    corrected = Grade("数学", 2025, "春期", 3, "A", 4, "Ｂ群 / 数学")

    assert grade_key(before) == grade_key(corrected) == "grade:2025:春期:数学"


def test_academic_sync_keeps_separate_grade_category_and_chronological_gpa_label():
    notion = RowsNotion()
    AcademicSync(notion, STATE, WASEDA).sync(record(
        [Grade("数学", 2024, "春期", 2, "A", 4, "Ｂ群 / 数学")], gpa=[GPAEntry("2024年度（春学期）", 2024, 3.06, "春学期")]))

    assert notion.rows["grades"][0]["properties"]["科目群"]["rich_text"][0]["text"]["content"] == "Ｂ群"
    assert notion.rows["grades"][0]["properties"]["科目区分"]["rich_text"][0]["text"]["content"] == "数学"
    assert notion.rows["gpa"][0]["properties"]["期間"]["title"][0]["text"]["content"] == "2024 1 春学期"


def test_academic_sync_upserts_same_record_without_duplicate_pages():
    """2回目の同期は、同じ行を作らず、変わった要件だけを決まった鍵で書き直す。"""
    notion = RowsNotion()
    sync = AcademicSync(notion, STATE, WASEDA)
    first = sync.sync(record([MATH], [Requirement("総合計", "", 124, 10, 10, 114, "総合計")],
                             [GPAEntry("2025年度（春学期）", 2025, 4, "春学期")]))
    second = sync.sync(record([MATH], [Requirement("総合計", "", 124, 10, 10, 114, "総合計")],
                              [GPAEntry("2025年度（春学期）", 2025, 4, "春学期")]))
    assert first.created == {"grades": 1, "requirements": 1, "gpa": 1}
    assert second.created == {"grades": 0, "requirements": 0, "gpa": 0}

    updated = sync.sync(record(requirements=[Requirement("総合計", "", 124, 12, 12, 112, "総合計")]))
    assert updated.updated == {"grades": 0, "requirements": 1, "gpa": 0}
    assert updated.created == {"grades": 0, "requirements": 0, "gpa": 0}


def test_ambiguous_course_is_reported_without_grade_relation():
    notion = RowsNotion({"courses": [course("math-a"), course("math-b")]})
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert result.ambiguous_relations == ("成績履歴: 数学 / 2025 / 春期",)


def test_academic_sync_preserves_an_existing_grade_course_relation():
    notion = RowsNotion({
        "courses": [course("matched-course")],
        "grades": [{"id": "grade-1", "properties": {
            "授業名": {"title": [{"plain_text": "数学"}]},
            "Kei Agent 成績ID": {"rich_text": [{"plain_text": "grade:2025:春期:数学"}]},
            "取得年度": {"number": 2025}, "学期": {"select": {"name": "春期"}},
            "単位": {"number": 2}, "成績": {"rich_text": [{"plain_text": "A"}]},
            "GP": {"number": 4}, "科目群": {"rich_text": [{"plain_text": "基礎"}]},
            "科目区分": {"rich_text": []},
            "授業": {"relation": [{"id": "manually-linked-course"}]},
        }}],
    }, writable=False)
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert result.unchanged == {"grades": 1, "requirements": 0, "gpa": 0}


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
    record = AcademicRecord(grades=(), requirements=(), gpa=())
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
    record = AcademicRecord(grades=(), requirements=(), gpa=())
    _school_from_config(monkeypatch, record)
    monkeypatch.setattr(academic_sync, "read_state", lambda: (_ for _ in ()).throw(AssertionError("state")))

    with pytest.raises(SystemExit, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        academic_sync.main([str(tmp_path / "grades.html"), str(tmp_path / "credits.html"), "--apply"])


@pytest.mark.parametrize("grades, in_notion", [
    ([MATH, Grade("数学", 2025, "春期", 2, "B", 3, "基礎")], []),          # 読んだ成績の中で重なる
    ([MATH], ["a", "b"]),                                                  # Notion にもう2行ある
])
def test_duplicate_record_keys_stop_before_any_write(grades, in_notion):
    row = {"properties": {"Kei Agent 成績ID": {"rich_text": [{"plain_text": "grade:2025:春期:数学"}]}}}
    notion = RowsNotion({"grades": [{"id": page_id, **row} for page_id in in_notion]})

    with pytest.raises(ValueError, match="重複"):
        AcademicSync(notion, STATE, WASEDA).sync(record(grades))

    assert notion.writes == []


def test_each_database_is_read_once_per_run():
    notion = RowsNotion()
    AcademicSync(notion, STATE, WASEDA).sync(record(
        [Grade(f"科目{i}", 2025, "春期", 2, "A", 4, "基礎") for i in range(5)],
        [Requirement(f"要件{i}", "", 2, 2, 2, 0, "区分") for i in range(3)],
        [GPAEntry("2025年度（春学期）", 2025, 3.5, "春学期")],
    ))

    assert sorted(notion.reads) == ["courses", "gpa", "grades", "requirements"]


def test_grade_matches_a_course_whose_name_differs_only_in_width():
    """全角・半角の違いだけの科目名は、同じ科目として結ぶ。"""
    notion = RowsNotion({"courses": [course("course-b", "情報セキュリティB", term="秋学期")]})

    AcademicSync(notion, STATE, WASEDA).sync(record([Grade("情報セキュリティＢ", 2025, "秋期", 2, "A", 4, "基礎")]))

    (_method, _path, body), = notion.writes
    assert body["properties"]["授業"] == {"relation": [{"id": "course-b"}]}
