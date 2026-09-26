from datetime import datetime

from kei_agent_course import notion_sync
from kei_agent_course.catalog import compare_course_catalog
from kei_agent_course.ics import Event


def _title(text: str) -> dict:
    return {"title": [{"plain_text": text}]}


class FakeNotion:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def paginate(self, method: str, path: str, body: dict):
        self.calls.append((method, path, body))
        return [
            {"id": "course-1", "properties": {"科目名": _title("情報通信ネットワークB")}},
            {"id": "course-2", "properties": {"科目名": _title("既知の科目")}},
        ]


def test_catalog_deduplicates_ics_course_names_without_writes():
    events = [
        Event("a", "提出期限", datetime(2026, 10, 1), "情報通信ネットワークB"),
        Event("b", "提出期限", datetime(2026, 10, 2), "情報通信ネットワークB"),
        Event("c", "提出期限", datetime(2026, 10, 3), "統計解析実習"),
    ]

    report = compare_course_catalog(events, {"情報通信ネットワークB"})

    assert report.registered == ("情報通信ネットワークB", "統計解析実習")
    assert report.known == ("情報通信ネットワークB",)
    assert report.missing == ("統計解析実習",)


def test_catalog_inspection_never_calls_notion_request_with_write_method():
    notion = FakeNotion()

    names = notion_sync.course_catalog(notion, {"databases": {"courses": {"data_source_id": "courses"}}})

    assert names == ("情報通信ネットワークB", "既知の科目")
    assert all(method == "POST" and path.endswith("/query") for method, path, _ in notion.calls)


def test_catalog_cli_needs_the_gateway_before_reading_moodle(monkeypatch):
    import pytest

    from kei_agent_course import catalog, moodle

    monkeypatch.setenv("MOODLE_ICS_URL", "https://example.invalid/calendar.ics")
    monkeypatch.setattr(moodle, "events", lambda url: (_ for _ in ()).throw(AssertionError("moodle")))
    with pytest.raises(SystemExit, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        catalog.main()
