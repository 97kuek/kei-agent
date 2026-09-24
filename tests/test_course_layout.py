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
    def __init__(self, views=(), view_details=None, view_pages=None, assignments=(), page_children=None):
        self.views = list(views)
        self.view_details = dict(view_details or {})
        self.view_pages = list(view_pages or [])
        self.assignments = list(assignments)
        self.page_children = dict(page_children or {})
        self.calls = []

    @property
    def writes(self):
        return [(method, path, body) for method, path, body in self.calls if method in {"POST", "PATCH"}]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/views?"):
            if self.view_pages:
                return self.view_pages[1] if "start_cursor=" in path else self.view_pages[0]
            return {"results": self.views}
        if method == "GET" and path.startswith("/views/"):
            return self.view_details[path.rsplit("/", 1)[-1]]
        if method == "GET" and path.startswith("/blocks/"):
            return {"results": self.page_children.get(path.split("/")[2], [])}
        if method == "PATCH" and path.startswith("/blocks/"):
            self.page_children.setdefault(path.split("/")[2], []).extend(body["children"])
            return {"results": body["children"]}
        if method == "PATCH" and path.startswith("/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            row = next(row for row in self.assignments if row["id"] == page_id)
            row["properties"].update(body["properties"])
            return row
        return {"id": "created-view"}

    def paginate(self, _method, path, _body=None):
        assert path == "/data_sources/assignment-source/query"
        return self.assignments

    def calls_for(self, method, path):
        return [call for call in self.calls if call[:2] == (method, path)]

    def page_title(self, page_id):
        row = next(row for row in self.assignments if row["id"] == page_id)
        title = row["properties"]["課題"]["title"][0]
        return title.get("plain_text") or title["text"]["content"]

    def appended_children(self, page_id):
        return [block["heading_2"]["rich_text"][0]["text"]["content"]
                for block in self.page_children.get(page_id, []) if block.get("type") == "heading_2"]


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


def test_apply_reads_view_details_when_the_list_omits_the_name():
    notion = FakeNotion(
        views=[{"id": "assignment-view"}],
        view_details={"assignment-view": {"id": "assignment-view", "name": "課題一覧"}},
    )
    apply_layout(notion, full_state(), apply=True)

    assert notion.calls_for("PATCH", "/views/assignment-view")
    created, = notion.calls_for("POST", "/views")
    assert created[2]["name"] == "GPA推移"


def test_apply_finds_a_named_view_on_a_later_list_page():
    notion = FakeNotion(view_pages=[
        {"results": [{"id": "first-view", "name": "別のview"}], "has_more": True, "next_cursor": "next-page"},
        {"results": [{"id": "assignment-view", "name": "課題一覧"}], "has_more": False, "next_cursor": None},
    ])
    apply_layout(notion, full_state(), apply=True)

    assert notion.calls_for("PATCH", "/views/assignment-view")
    created, = notion.calls_for("POST", "/views")
    assert created[2]["name"] == "GPA推移"


def assignment(page_id, title):
    return {"id": page_id, "properties": {"課題": {"title": [{"plain_text": title}]}}}


def test_reconcile_updates_only_exact_moodle_titles_and_empty_pages():
    from kei_agent_course.course_layout import reconcile_assignment_pages

    notion = FakeNotion(
        assignments=[assignment("p1", "「課題#1」の提出期限"), assignment("p2", "アンケート終了")],
        page_children={"p1": [], "p2": [{"type": "paragraph"}]},
    )
    report = reconcile_assignment_pages(notion, full_state(), apply=True)

    assert report.renamed == 1 and report.templated == 1
    assert notion.page_title("p1") == "課題#1"
    assert notion.page_title("p2") == "アンケート終了"
    assert notion.appended_children("p2") == []
