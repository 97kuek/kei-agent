"""成績を単位要件と結ぶところ（requirement_changes・link_requirements）。"""

import pytest

from kei_agent_modules.course import school
from kei_agent_modules.course.academic_sync import link_requirements, requirement_changes

# 単位要件の名前の対応は、早稲田の部品のもの
WASEDA = school.load({"school": "waseda"})


def text(value):
    return {"rich_text": [{"plain_text": value}]}


def title(value):
    return {"title": [{"plain_text": value}]}


def requirement(page_id, group, name):
    return {"id": page_id, "properties": {"Group": text(group), "Name": title(name)}}


def grade(page_id, group, category, linked=()):
    return {"id": page_id, "properties": {"Group": text(group), "Category": text(category),
                                          "Requirement": {"relation": list(linked)}}}


@pytest.mark.parametrize("linked, expected", [
    ([], {"grade": {"Requirement": {"relation": [{"id": "requirement"}]}}}),
    ([{"id": "manual"}], {}),   # 手で結んだ要件は置き換えない
])
def test_only_an_exact_requirement_is_linked_and_a_manual_link_is_kept(linked, expected):
    rows = {"grades": [grade("grade", "Ｂ群", "数学", linked)],
            "requirements": [requirement("requirement", "Ｂ群", "数学")]}
    assert requirement_changes(rows, WASEDA) == expected


def test_two_requirements_with_the_same_name_are_not_guessed():
    rows = {"grades": [grade("g", "Ｂ群", "数学")],
            "requirements": [requirement("r1", "Ｂ群", "数学"), requirement("r2", "Ｂ群", "数学")]}
    assert requirement_changes(rows, WASEDA) == {}


@pytest.mark.parametrize(("group", "category", "requirement_name"), [
    ("Ａ群", "外国語 英語", "外国語 英語 必修"),
    ("Ｂ群", "自然科学 物理学", "自然科学 物理学 必修"),
    ("Ｂ群", "自然科学 化学", "自然科学 化学 必修"),
    ("Ｃ群(専門教育科目)", "専門選択必修", "専門選択必修（学系別専門）"),
])
def test_verified_parent_requirement_links(group, category, requirement_name):
    rows = {"grades": [grade("g", group, category)], "requirements": [requirement("r", group, requirement_name)]}
    assert requirement_changes(rows, WASEDA) == {"g": {"Requirement": {"relation": [{"id": "r"}]}}}


def test_link_requirements_writes_only_the_grade_side():
    """成績の Requirement に書く。単位要件の Grades は Notion が戻り側として持つので書かない。"""
    class Notion:
        def __init__(self):
            self.writes = []

        def paginate(self, method, path, body=None):
            return {"grades": [grade("g", "Ｂ群", "数学")],
                    "requirements": [requirement("r", "Ｂ群", "数学")]}[path.split("/")[2]]

        def request(self, method, path, body=None):
            self.writes.append((method, path, body))
            return {}

    notion = Notion()
    state = {"databases": {"grades": {"data_source_id": "grades"},
                           "requirements": {"data_source_id": "requirements"}}}
    assert link_requirements(notion, state, WASEDA) == 1
    assert notion.writes == [("PATCH", "/pages/g", {"properties": {"Requirement": {"relation": [{"id": "r"}]}}})]
