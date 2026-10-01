"""成績から過去授業を作り、要件・GPA と結ぶところ。"""

import pytest

from kei_agent_modules.course import school
from kei_agent_modules.course.academic_sync import academic_relation_changes, historical_course_changes

# 学期の直し方・GPA の期間・単位要件の名前の対応は、早稲田の部品のもの
WASEDA = school.load({"school": "waseda"})


def text(value):
    return {"rich_text": [{"plain_text": value}]}


def title(value):
    return {"title": [{"plain_text": value}]}


def requirement(page_id, group, name):
    return {"id": page_id, "properties": {"大区分": text(group), "要件名": title(name)}}


def gpa(page_id, year, kind):
    return {"id": page_id, "properties": {"年度": {"number": year}, "種別": {"select": {"name": kind}}}}


def course(page_id, name, year=2024, term="春学期"):
    return {"id": page_id, "properties": {"科目名": title(name), "年度": {"number": year},
                                          "学期": {"select": {"name": term}}}}


@pytest.mark.parametrize("linked, expected", [
    ([], {"grade": {"単位要件": {"relation": [{"id": "requirement"}]}, "GPA推移": {"relation": [{"id": "gpa"}]}}}),
    ([{"id": "manual"}], {"grade": {"GPA推移": {"relation": [{"id": "gpa"}]}}}),   # 手で結んだ要件は置き換えない
])
def test_only_exact_grade_requirement_and_term_gpa_relations_are_added(linked, expected):
    rows = {
        "grades": [{"id": "grade", "properties": {
            "科目群": text("Ｂ群"), "科目区分": text("数学"),
            "取得年度": {"number": 2025}, "学期": {"select": {"name": "春期"}},
            "単位要件": {"relation": linked}, "GPA推移": {"relation": []},
        }}],
        "requirements": [requirement("requirement", "Ｂ群", "数学")],
        "gpa": [gpa("gpa", 2025, "春学期")],
    }

    assert academic_relation_changes(rows, WASEDA) == expected


def test_historical_course_is_planned_once_with_grade_and_verified_relations():
    grade = {"id": "g1", "properties": {
        "授業名": title("基礎物理学Ａ"),
        "取得年度": {"number": 2024}, "学期": {"select": {"name": "春期"}},
        "単位": {"number": 2}, "科目群": text("Ｂ群"), "科目区分": text("自然科学 物理学"),
        "授業": {"relation": []}, "単位要件": {"relation": [{"id": "r1"}]},
        "GPA推移": {"relation": [{"id": "p1"}]},
    }}
    planned = historical_course_changes([grade], [], WASEDA)
    assert planned[0]["grade_id"] == "g1"
    assert planned[0]["properties"]["年度"] == {"number": 2024}
    assert planned[0]["properties"]["学期"] == {"select": {"name": "春学期"}}
    assert planned[0]["properties"]["単位要件"] == {"relation": [{"id": "r1"}]}
    assert historical_course_changes([grade], [course("c1", "基礎物理学Ａ")], WASEDA) == []


def test_historical_course_identity_collision_stops_before_write():
    grade = {"id": "g1", "properties": {
        "授業名": title("情報数学"),
        "取得年度": {"number": 2024}, "学期": {"select": {"name": "春期"}},
        "単位": {"number": 2}, "授業": {"relation": []},
    }}
    with pytest.raises(ValueError, match="重複"):
        historical_course_changes([grade], [course(key, "情報数学") for key in ("c1", "c2")], WASEDA)


@pytest.mark.parametrize(("group", "category", "requirement_name"), [
    ("Ａ群", "外国語 英語", "外国語 英語 必修"),
    ("Ｂ群", "自然科学 物理学", "自然科学 物理学 必修"),
    ("Ｂ群", "自然科学 化学", "自然科学 化学 必修"),
    ("Ｃ群(専門教育科目)", "専門選択必修", "専門選択必修（学系別専門）"),
])
def test_verified_parent_requirement_links(group, category, requirement_name):
    rows = {"grades": [{"id": "g", "properties": {
        "科目群": text(group), "科目区分": text(category), "単位要件": {"relation": []},
    }}], "requirements": [requirement("r", group, requirement_name)], "gpa": []}
    assert academic_relation_changes(rows, WASEDA) == {"g": {"単位要件": {"relation": [{"id": "r"}]}}}


def test_quarter_and_annual_gpa_mapping_leaves_unverified_winter_unlinked():
    rows = {"grades": [{"id": term, "properties": {
        "授業名": title("データ科学入門α ０１" if term == "その他" else term),
        "取得年度": {"number": 2025}, "学期": {"select": {"name": term}},
        "GPA推移": {"relation": []},
    }} for term in ("夏ク", "秋ク", "冬ク", "通年", "その他")],
        "requirements": [], "gpa": [gpa(kind, 2025, kind) for kind in ("春学期", "秋学期")]}
    changes = academic_relation_changes(rows, WASEDA)
    assert changes["夏ク"]["GPA推移"]["relation"] == [{"id": "春学期"}]
    assert changes["秋ク"]["GPA推移"]["relation"] == [{"id": "秋学期"}]
    assert changes["通年"]["GPA推移"]["relation"] == [{"id": "秋学期"}]
    assert changes["その他"]["GPA推移"]["relation"] == [{"id": "春学期"}]
    assert "冬ク" not in changes
