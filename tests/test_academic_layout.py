import pytest

from kei_agent_course.academic_layout import (
    academic_page_changes,
    academic_relation_changes,
    duplicate_course_to_archive,
    historical_course_changes,
)


def test_existing_academic_rows_gain_stable_ids_and_separate_grade_categories():
    rows = {
        "grades": [{"id": "grade-1", "properties": {
            "科目名": {"title": [{"plain_text": "情報数学"}]},
            "取得年度": {"number": 2025}, "学期": {"select": {"name": "春期"}},
            "単位": {"number": 2}, "成績": {"rich_text": [{"plain_text": "A"}]},
            "科目区分": {"rich_text": [{"plain_text": "Ｂ群 / 数学"}]},
            "Kei Agent 成績ID": {"rich_text": []}, "取得済": {"checkbox": True},
        }}],
        "requirements": [{"id": "req-1", "properties": {
            "要件名": {"title": [{"plain_text": "小計"}]},
            "大区分": {"rich_text": [{"plain_text": "Ｂ群"}]},
            "Kei Agent 要件ID": {"rich_text": []},
        }}],
        "gpa": [{"id": "gpa-1", "properties": {
            "期間": {"title": [{"plain_text": "2024年度（春学期）"}]},
            "年度": {"number": 2024}, "種別": {"select": {"name": "春学期"}},
            "Kei Agent GPAID": {"rich_text": []},
        }}],
    }

    changes = academic_page_changes(rows)

    assert changes["grades"]["grade-1"] == {
        "Kei Agent 成績ID": {"rich_text": [{"text": {"content": "grade:2025:春期:情報数学"}}]},
        "科目群": {"rich_text": [{"text": {"content": "Ｂ群"}}]},
        "科目区分": {"rich_text": [{"text": {"content": "数学"}}]},
    }
    assert changes["requirements"]["req-1"] == {
        "Kei Agent 要件ID": {"rich_text": [{"text": {"content": "requirement:Ｂ群:小計"}}]},
    }
    assert changes["gpa"]["gpa-1"] == {
        "Kei Agent GPAID": {"rich_text": [{"text": {"content": "gpa:2024:春学期"}}]},
        "期間": {"title": [{"text": {"content": "2024 1 春学期"}}]},
    }


def test_conflicting_academic_id_is_rejected_before_any_write():
    rows = {"grades": [], "requirements": [], "gpa": [
        {"id": key, "properties": {
            "期間": {"title": [{"plain_text": "2025年度（秋学期）"}]},
            "年度": {"number": 2025}, "種別": {"select": {"name": "秋学期"}},
            "Kei Agent GPAID": {"rich_text": []},
        }} for key in ("first", "second")
    ]}

    with pytest.raises(ValueError, match="重複"):
        academic_page_changes(rows)


def test_grade_migration_is_safe_after_title_rename():
    rows = {"grades": [{"id": "g", "properties": {
        "授業名": {"title": [{"plain_text": "情報数学"}]},
        "取得年度": {"number": 2025}, "学期": {"select": {"name": "春期"}},
        "単位": {"number": 2}, "成績": {"rich_text": [{"plain_text": "A"}]},
        "科目群": {"rich_text": [{"plain_text": "Ｂ群"}]},
        "科目区分": {"rich_text": [{"plain_text": "数学"}]},
        "Kei Agent 成績ID": {"rich_text": [{"plain_text": "grade:2025:春期:情報数学"}]},
    }}], "requirements": [], "gpa": []}

    assert academic_page_changes(rows)["grades"] == {}


def test_only_empty_unscheduled_security_duplicate_can_be_archived():
    def row(key, name, day, period, relations=()):
        return {"id": key, "properties": {
            "科目名": {"title": [{"plain_text": name}]},
            "曜日": {"select": {"name": day}}, "時限": {"number": period},
            "学期": {"select": {"name": "秋学期"}},
            "課題": {"relation": [{"id": item} for item in relations]},
        }}

    canonical = row("scheduled", "情報セキュリティB", "金", 2)
    duplicate = row("empty", "情報セキュリティＢ", "他", None)
    assert duplicate_course_to_archive([canonical, duplicate]) == "empty"
    assert duplicate_course_to_archive([canonical, row("linked", "情報セキュリティＢ", "他", None, ("task",))]) is None


def test_layout_moves_empty_duplicate_to_recoverable_trash():
    from kei_agent_course.academic_layout import reconcile_academic_layout

    course_rows = [{"id": key, "properties": {
        "科目名": {"title": [{"plain_text": name}]},
        "曜日": {"select": {"name": day}}, "時限": {"number": period},
    }} for key, name, day, period in (
        ("scheduled", "情報セキュリティB", "金", 2),
        ("duplicate", "情報セキュリティＢ", "他", None),
    )]

    class Notion:
        def __init__(self):
            self.calls = []

        def paginate(self, _method, path, _body):
            return course_rows if path == "/data_sources/courses/query" else []

        def request(self, method, path, body=None):
            self.calls.append((method, path, body))
            if path.startswith("/blocks/"):
                return {"results": []}
            if path.startswith("/views?"):
                return {"results": []}
            return {}

    state = {"databases": {key: {"database_id": key, "data_source_id": key,
                                "properties": {name: name for name in names}} for key, names in {
        "grades": ("科目群", "科目区分", "授業名", "成績", "GP", "単位", "取得年度", "取得済"),
        "requirements": ("大区分", "要件名", "所定単位", "既得単位", "算入単位", "残り単位", "集計種別"),
        "courses": ("科目名", "科目群", "必選区分", "年度", "学期", "曜日", "時限", "単位", "状態"),
        "gpa": ("期間", "GPA", "種別", "年度"),
    }.items()}}
    notion = Notion()

    reconcile_academic_layout(notion, state, apply=True)

    assert ("PATCH", "/pages/duplicate", {"in_trash": True}) in notion.calls


def test_only_exact_grade_requirement_and_term_gpa_relations_are_added():
    rows = {
        "grades": [{"id": "grade", "properties": {
            "科目群": {"rich_text": [{"plain_text": "Ｂ群"}]},
            "科目区分": {"rich_text": [{"plain_text": "数学"}]},
            "取得年度": {"number": 2025}, "学期": {"select": {"name": "春期"}},
            "単位要件": {"relation": []}, "GPA推移": {"relation": []},
        }}],
        "requirements": [{"id": "requirement", "properties": {
            "大区分": {"rich_text": [{"plain_text": "Ｂ群"}]},
            "要件名": {"title": [{"plain_text": "数学"}]},
        }}],
        "gpa": [{"id": "gpa", "properties": {
            "年度": {"number": 2025}, "種別": {"select": {"name": "春学期"}},
        }}],
    }

    assert academic_relation_changes(rows) == {"grade": {
        "単位要件": {"relation": [{"id": "requirement"}]},
        "GPA推移": {"relation": [{"id": "gpa"}]},
    }}


def test_existing_manual_relation_is_not_replaced():
    rows = {"grades": [{"id": "grade", "properties": {
        "科目群": {"rich_text": [{"plain_text": "Ｂ群"}]},
        "科目区分": {"rich_text": [{"plain_text": "数学"}]},
        "取得年度": {"number": 2025}, "学期": {"select": {"name": "春期"}},
        "単位要件": {"relation": [{"id": "manual"}]},
        "GPA推移": {"relation": []},
    }}], "requirements": [{"id": "req", "properties": {
        "大区分": {"rich_text": [{"plain_text": "Ｂ群"}]},
        "要件名": {"title": [{"plain_text": "数学"}]},
    }}], "gpa": []}

    assert academic_relation_changes(rows) == {}


def test_historical_course_is_planned_once_with_grade_and_verified_relations():
    grade = {"id": "g1", "properties": {
        "授業名": {"title": [{"plain_text": "基礎物理学Ａ"}]},
        "取得年度": {"number": 2024}, "学期": {"select": {"name": "春期"}},
        "単位": {"number": 2}, "科目群": {"rich_text": [{"plain_text": "Ｂ群"}]},
        "科目区分": {"rich_text": [{"plain_text": "自然科学 物理学"}]},
        "授業": {"relation": []}, "単位要件": {"relation": [{"id": "r1"}]},
        "GPA推移": {"relation": [{"id": "p1"}]},
    }}
    planned = historical_course_changes([grade], [])
    assert planned[0]["grade_id"] == "g1"
    assert planned[0]["properties"]["年度"] == {"number": 2024}
    assert planned[0]["properties"]["学期"] == {"select": {"name": "春学期"}}
    assert planned[0]["properties"]["単位要件"] == {"relation": [{"id": "r1"}]}
    existing = {"id": "c1", "properties": {
        "科目名": {"title": [{"plain_text": "基礎物理学Ａ"}]},
        "年度": {"number": 2024}, "学期": {"select": {"name": "春学期"}},
    }}
    assert historical_course_changes([grade], [existing]) == []


def test_historical_course_identity_collision_stops_before_write():
    grade = {"id": "g1", "properties": {
        "授業名": {"title": [{"plain_text": "情報数学"}]},
        "取得年度": {"number": 2024}, "学期": {"select": {"name": "春期"}},
        "単位": {"number": 2}, "授業": {"relation": []},
    }}
    with pytest.raises(ValueError, match="重複"):
        historical_course_changes([grade], [{"id": key, "properties": {
            "科目名": {"title": [{"plain_text": "情報数学"}]},
            "年度": {"number": 2024}, "学期": {"select": {"name": "春学期"}},
        }} for key in ("c1", "c2")])


@pytest.mark.parametrize(("group", "category", "requirement_name"), [
    ("Ａ群", "外国語 英語", "外国語 英語 必修"),
    ("Ｂ群", "自然科学 物理学", "自然科学 物理学 必修"),
    ("Ｂ群", "自然科学 化学", "自然科学 化学 必修"),
    ("Ｃ群(専門教育科目)", "専門選択必修", "専門選択必修（学系別専門）"),
])
def test_verified_parent_requirement_links(group, category, requirement_name):
    rows = {"grades": [{"id": "g", "properties": {
        "科目群": {"rich_text": [{"plain_text": group}]},
        "科目区分": {"rich_text": [{"plain_text": category}]},
        "単位要件": {"relation": []},
    }}], "requirements": [{"id": "r", "properties": {
        "大区分": {"rich_text": [{"plain_text": group}]},
        "要件名": {"title": [{"plain_text": requirement_name}]},
    }}], "gpa": []}
    assert academic_relation_changes(rows) == {"g": {
        "単位要件": {"relation": [{"id": "r"}]},
    }}


def test_quarter_and_annual_gpa_mapping_leaves_unverified_winter_unlinked():
    rows = {"grades": [{"id": term, "properties": {
        "授業名": {"title": [{"plain_text": "データ科学入門α ０１" if term == "その他" else term}]},
        "取得年度": {"number": 2025}, "学期": {"select": {"name": term}},
        "GPA推移": {"relation": []},
    }} for term in ("夏ク", "秋ク", "冬ク", "通年", "その他")],
        "requirements": [], "gpa": [{"id": kind, "properties": {
            "年度": {"number": 2025}, "種別": {"select": {"name": kind}},
        }} for kind in ("春学期", "秋学期")]}
    changes = academic_relation_changes(rows)
    assert changes["夏ク"]["GPA推移"]["relation"] == [{"id": "春学期"}]
    assert changes["秋ク"]["GPA推移"]["relation"] == [{"id": "秋学期"}]
    assert changes["通年"]["GPA推移"]["relation"] == [{"id": "秋学期"}]
    assert changes["その他"]["GPA推移"]["relation"] == [{"id": "春学期"}]
    assert "冬ク" not in changes
