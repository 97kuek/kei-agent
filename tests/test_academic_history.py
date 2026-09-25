"""成績から過去授業を作り、要件・GPA と結ぶところ。"""

import pytest

from kei_agent_course.academic_sync import academic_relation_changes, historical_course_changes


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
