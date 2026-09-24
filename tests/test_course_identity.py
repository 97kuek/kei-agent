import pytest

from kei_agent_course.catalog import compare_course_catalog
from kei_agent_course.course_identity import normalize_course_name
from kei_agent_course.notion_setup import CourseSetup
from kei_agent_course.notion_sync import CourseNotion, SyncError


def test_fullwidth_and_ascii_course_suffixes_have_one_identity():
    assert normalize_course_name("情報セキュリティＢ") == "情報セキュリティB"
    assert normalize_course_name(" 情報セキュリティB ") == "情報セキュリティB"


def test_catalog_recognizes_a_moodle_course_with_fullwidth_suffix():
    class Event:
        course_name = "情報セキュリティＢ"

    report = compare_course_catalog([Event()], ["情報セキュリティB"])

    assert report.known == ("情報セキュリティＢ",)
    assert report.missing == ()


def test_course_lookup_refuses_ambiguous_normalized_duplicates():
    class Notion:
        def paginate(self, *_args):
            return [{"id": key, "properties": {"科目名": {"title": [{"plain_text": name}]}}}
                    for key, name in (("first", "情報セキュリティB"), ("second", "情報セキュリティＢ"))]

    state = {"databases": {"courses": {"data_source_id": "courses"},
                           "assignments": {"data_source_id": "assignments"}}}
    with pytest.raises(SyncError, match="重複"):
        CourseNotion(Notion(), state).course_ids()


def test_course_lookup_ignores_completed_historical_course():
    class Notion:
        def paginate(self, *_args):
            return [{"id": key, "properties": {
                "科目名": {"title": [{"plain_text": "情報セキュリティB"}]},
                "状態": {"select": {"name": status}},
            }} for key, status in (("old", "終了"), ("current", "履修中"))]

    state = {"databases": {"courses": {"data_source_id": "courses"},
                           "assignments": {"data_source_id": "assignments"}}}
    assert CourseNotion(Notion(), state).course_ids() == {"情報セキュリティB": "current"}


def test_course_setup_does_not_seed_a_fullwidth_variant_again(tmp_path):
    class Notion:
        def paginate(self, method, path, _body):
            assert (method, path) == ("POST", "/data_sources/courses/query")
            return [{"id": "existing", "properties": {"科目名": {"title": [
                {"plain_text": "情報セキュリティＢ"}]}}}]

        def request(self, method, path, _body):
            raise AssertionError((method, path))

    setup = CourseSetup(Notion(), "home", tmp_path / "state.json")
    setup.state = {"databases": {"courses": {"data_source_id": "courses"}}}

    setup.add_course("情報セキュリティB", "金", 2)
