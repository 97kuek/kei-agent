from kei_agent_course.course_layout import apply_layout, assignment_view_payload, gpa_view_payload


def full_state():
    return {"databases": {
        "assignments": {
            "database_id": "assignment-db", "data_source_id": "assignment-source",
            "properties": {"科目": "a-course", "課題": "a-title", "締切": "a-due", "状態": "a-status",
                           "Moodle ID": "a-moodle-id", "見積時間": "a-estimate"},
        },
        "gpa": {
            "database_id": "gpa-db", "data_source_id": "gpa-source",
            "properties": {"期間": "g-period", "年度": "g-year", "種別": "g-kind", "GPA": "g-value"},
        },
    }}


def test_assignment_view_shows_only_the_requested_columns_in_order():
    payload = assignment_view_payload(full_state())
    columns = payload["configuration"]["properties"]

    assert [column["property_id"] for column in columns[:4]] == ["a-course", "a-title", "a-due", "a-status"]
    assert all(column["visible"] for column in columns[:4])
    assert all(not column["visible"] for column in columns[4:])
    assert payload["sorts"] == [{"property": "a-due", "direction": "ascending"}]


def test_gpa_view_is_a_line_chart_of_raw_period_and_gpa_values():
    payload = gpa_view_payload(full_state())
    config = payload["configuration"]

    assert config["chart_type"] == "line"
    assert config["x_axis_property_id"] == "g-period"
    assert config["y_axis_property_id"] == "g-value"
    assert config["sort"] == "x_ascending"


class FakeNotion:
    def __init__(self, views=()):
        self.views = list(views)
        self.calls = []

    @property
    def writes(self):
        return [(method, path, body) for method, path, body in self.calls if method in {"POST", "PATCH"}]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/views?"):
            return {"results": self.views}
        return {"id": "created-view"}

    def calls_for(self, method, path):
        return [call for call in self.calls if call[:2] == (method, path)]


def test_dry_run_never_mutates_notion():
    notion = FakeNotion()
    report = apply_layout(notion, full_state(), apply=False)

    assert report.planned == ("課題一覧: create", "GPA推移: create")
    assert notion.writes == []


def test_apply_updates_existing_named_view_without_duplicate():
    notion = FakeNotion(views=[{"id": "assignment-view", "name": "課題一覧"}])
    apply_layout(notion, full_state(), apply=True)

    assert notion.calls_for("PATCH", "/views/assignment-view")
    created, = notion.calls_for("POST", "/views")
    assert created[2]["name"] == "GPA推移"
