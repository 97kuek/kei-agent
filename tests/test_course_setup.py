"""授業ホームの作り（授業時間表のページと4つの DB の列・順番・選択肢）。本物の Notion は使わない。"""

import pytest

from kei_agent.storage.notion import Notion, NotionError
from kei_agent.testing.fakes import FakeNotionAPI
from kei_agent_modules.course import notion_setup, school
from kei_agent_modules.course.notion_setup import CourseSetup

WASEDA = school.load({"school": "waseda"})
COURSE_COLUMNS = ["Name", "Status", "Year", "Term", "Day", "Period", "Credits", "Moodle"]
ASSIGNMENT_COLUMNS = ["Name", "Status", "Due", "Course", "Link", "Moodle ID"]
GRADE_COLUMNS = ["Name", "Grade", "GP", "Credits", "Category", "Group", "Course", "Requirement", "Record ID"]
REQUIREMENT_COLUMNS = ["Name", "Remaining", "Required", "Counted", "Group", "Kind", "Grades", "Record ID"]


def client(api: FakeNotionAPI):
    """FakeNotionAPI に、Notion と同じ paginate と children を付ける。"""
    class Client:
        request = staticmethod(api.request)

        def paginate(self, method, path, body=None):
            return Notion.paginate(self, method, path, body)

        def children(self, block_id):
            return Notion.children(self, block_id)

    return Client()


def options(spec: dict, name: str) -> list[str]:
    kind = next(iter(spec["properties"][name]))
    return [option["name"] for option in spec["properties"][name][kind]["options"]]


def columns(api: FakeNotionAPI, setup: CourseSetup, key: str) -> list[str]:
    ds = setup.state["databases"][key]["data_source_id"]
    return list(api.items[api.key(ds)]["properties"])


def home_children(api: FakeNotionAPI, home: str) -> list[tuple[str, str]]:
    return [(b["type"], (b.get("child_page") or b.get("child_database") or {}).get("title"))
            for b in client(api).children(home)]


def new_home(tmp_path):
    api = FakeNotionAPI()
    home = api.add_page(title="授業ホーム")
    return api, home, CourseSetup(client(api), home, tmp_path / "notion-course.json", WASEDA)


def test_columns_and_options_follow_the_spec():
    assert list(notion_setup.COURSES["properties"]) == COURSE_COLUMNS
    assert options(notion_setup.COURSES, "Status") == ["Taking", "Done"]
    assert list(notion_setup.ASSIGNMENTS["properties"]) == ASSIGNMENT_COLUMNS
    assert options(notion_setup.ASSIGNMENTS, "Status") == ["Not started", "In progress", "Submitted", "Overdue"]
    assert list(notion_setup.GRADES["properties"]) == GRADE_COLUMNS
    assert list(notion_setup.REQUIREMENTS["properties"]) == REQUIREMENT_COLUMNS
    assert options(notion_setup.REQUIREMENTS, "Kind") == ["Category", "Subtotal", "Total", "Other"]
    assert [title for title, _spec in notion_setup.SPECS.values()] == ["授業", "課題", "成績", "単位要件"]


def test_setup_puts_the_timetable_first_and_creates_columns_in_order(tmp_path):
    api, home, setup = new_home(tmp_path)
    setup.run()

    assert home_children(api, home) == [("child_page", "授業時間表"), ("child_database", "授業"),
                                        ("child_database", "課題"), ("child_database", "成績"),
                                        ("child_database", "単位要件")]
    assert columns(api, setup, "courses") == COURSE_COLUMNS
    assert columns(api, setup, "assignments") == ASSIGNMENT_COLUMNS
    # 戻り側の Requirement は、単位要件の Grades を作ったときに Notion が成績の右端に足す
    assert columns(api, setup, "grades") == [*GRADE_COLUMNS[:7], "Record ID", "Requirement"]
    assert columns(api, setup, "requirements") == REQUIREMENT_COLUMNS
    assert set(setup.state["databases"]) == {"courses", "assignments", "grades", "requirements"}
    timetable = next(b["id"] for b in client(api).children(home) if b["type"] == "child_page")
    lines = [b["bulleted_list_item"]["rich_text"][0]["plain_text"] for b in client(api).children(timetable)]
    assert lines[:2] == ["1限 08:50-10:30", "2限 10:40-12:20"]


def test_course_links_are_one_way_and_grades_and_requirements_are_two_way(tmp_path):
    api, _home, setup = new_home(tmp_path)
    setup.run()
    props = {key: api.items[api.key(setup.state["databases"][key]["data_source_id"])]["properties"]
             for key in ("courses", "assignments", "grades", "requirements")}

    # 授業に戻り側の列を作らない（授業の列は8つだけ）
    assert not any(p["type"] == "relation" for p in props["courses"].values())
    course = props["assignments"]["Course"]["relation"]
    assert course["type"] == "single_property"
    assert course["data_source_id"] == setup.state["databases"]["courses"]["data_source_id"]
    assert props["grades"]["Course"]["relation"]["type"] == "single_property"
    assert props["requirements"]["Grades"]["relation"]["dual_property"]["synced_property_name"] == "Requirement"
    assert props["grades"]["Requirement"]["relation"]["dual_property"]["synced_property_name"] == "Grades"


def test_table_views_show_the_columns_in_the_spec_order_including_the_back_relation(tmp_path):
    api, home, setup = new_home(tmp_path)
    setup.run()
    db = setup.state["databases"]["grades"]
    view = api.add_view(db["database_id"], "Default view")

    setup.run()

    shown = api.request("GET", f"/views/{view}")["configuration"]["properties"]
    by_id = {p["id"]: name for name, p in api.items[api.key(db["data_source_id"])]["properties"].items()}
    assert [by_id[p["property_id"]] for p in shown] == GRADE_COLUMNS
    assert all(p["visible"] for p in shown)
    # 3回目は同じ形なので、ビューも DB もページも書き直さない
    before, items = len(setup.log), len(api.items)
    setup.run()
    assert setup.log[before:] == [] and len(api.items) == items
    assert [title for _kind, title in home_children(api, home)].count("授業時間表") == 1


@pytest.mark.parametrize(("legacy", "match"), [("title", "科目名"), ("grades", "📊 成績履歴")])
def test_setup_on_a_home_that_is_not_migrated_stops_before_any_write(tmp_path, legacy, match):
    api = FakeNotionAPI()
    home = api.add_page(title="授業ホーム")
    if legacy == "title":
        api.add_database(home, "授業", {"科目名": {"title": {}}, "状態": {"select": {"options": []}}})
    else:
        api.add_database(home, "📊 成績履歴", {"授業名": {"title": {}}})
    before = len(api.items)

    with pytest.raises(NotionError, match=match):
        CourseSetup(client(api), home, tmp_path / "notion-course.json", WASEDA).run()

    assert len(api.items) == before
    assert not (tmp_path / "notion-course.json").exists()


@pytest.mark.parametrize("duplicate", ["授業", "成績"])
def test_two_databases_with_the_same_name_stop_before_any_write(tmp_path, duplicate):
    api = FakeNotionAPI()
    home = api.add_page(title="授業ホーム")
    for _ in range(2):
        api.add_database(home, duplicate, {"Name": {"title": {}}})
    before = len(api.items)

    with pytest.raises(NotionError, match="重複"):
        CourseSetup(client(api), home, tmp_path / "notion-course.json", WASEDA).run()

    assert len(api.items) == before
