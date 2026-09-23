from kei_agent_course.academic_sync import inspect_course_home


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
