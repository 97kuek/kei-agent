import pytest

from kei_agent_modules.course.catalog import compare_course_catalog
from kei_agent_modules.course.course_identity import normalize_course_name
from kei_agent_modules.course.notion_setup import CourseSetup
from kei_agent_modules.course.notion_sync import CourseNotion, SyncError

STATE = {"databases": {"courses": {"data_source_id": "courses"}, "assignments": {"data_source_id": "assignments"}}}


class Notion:
    """「授業」の行だけを返す Notion の代わり。書き込みが来たら落ちる。"""

    def __init__(self, *rows):
        self.rows = [{"id": key, "properties": {"科目名": {"title": [{"plain_text": name}]},
                                               **({"状態": {"select": {"name": status}}} if status else {})}}
                     for key, name, status in rows]

    def paginate(self, method, path, _body=None):
        assert (method, path) == ("POST", "/data_sources/courses/query")
        return self.rows

    def request(self, method, path, _body=None):
        raise AssertionError((method, path))


def test_fullwidth_and_ascii_course_suffixes_have_one_identity():
    """全角と半角の「Ｂ/B」は同じ科目。Moodle の科目名も「授業」と突き合う。"""
    assert normalize_course_name("情報セキュリティＢ") == "情報セキュリティB"
    assert normalize_course_name(" 情報セキュリティB ") == "情報セキュリティB"

    class Event:
        course_name = "情報セキュリティＢ"

    report = compare_course_catalog([Event()], ["情報セキュリティB"])
    assert report.known == ("情報セキュリティＢ",) and report.missing == ()


def test_course_lookup_refuses_ambiguous_normalized_duplicates():
    notion = Notion(("first", "情報セキュリティB", None), ("second", "情報セキュリティＢ", None))
    with pytest.raises(SyncError, match="重複"):
        CourseNotion(notion, STATE).course_ids()


def test_course_lookup_ignores_completed_historical_course():
    notion = Notion(("old", "情報セキュリティB", "終了"), ("current", "情報セキュリティB", "履修中"))
    assert CourseNotion(notion, STATE).course_ids() == {"情報セキュリティB": "current"}


def test_course_setup_does_not_seed_a_fullwidth_variant_again(tmp_path):
    setup = CourseSetup(Notion(("existing", "情報セキュリティＢ", None)), "home", tmp_path / "state.json")
    setup.state = {"databases": {"courses": {"data_source_id": "courses"}}}

    setup.add_course("情報セキュリティB", "金", 2)
