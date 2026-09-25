"""Legacy Daily/Retro migration is audited before it can mutate either store."""

import pytest
from fakes import check_notion_body

from kei_agent.notion import NotionError
from kei_agent.notion_hub import RESEARCH_HOME_ID
from kei_agent.notion_hub_migration import (
    _copied,
    _entry_text,
    apply_legacy_notes,
    audit_legacy_notes,
    verify_manifest_current,
)
from kei_agent.store import Store


def rich(text):
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


class LegacyNotion:
    def __init__(self):
        self.pages = []
        self.blocks = {}
        self.writes = []
        self.allow_writes = False
        self.fail_move = False
        self.blocks["home"] = []
        self.blocks[RESEARCH_HOME_ID] = []

    def note(self, page_id, day, kind, body):
        self.pages.append({
            "id": page_id, "url": f"https://notion.so/{page_id}",
            "parent": {"type": "data_source_id", "data_source_id": "notes-ds"},
            "properties": {
                "タイトル": {"title": rich(f"{kind} {day}")},
                "種類": {"select": {"name": kind}},
                "日付": {"date": {"start": day}} if day else {"date": None},
                "Slack": {"url": f"https://slack.example/{page_id}"},
                "ファイル": {"rich_text": rich(f"reviews/{day}.md")},
            },
        })
        self.blocks[page_id] = [{"id": f"block-{page_id}", "type": "paragraph",
                                 "paragraph": {"rich_text": rich(body)}}]

    def paginate(self, method, path, body):
        assert (method, path) == ("POST", "/data_sources/notes-ds/query")
        return [page for page in self.pages if page["parent"].get("data_source_id") == "notes-ds"]

    def children(self, page_id):
        return self.blocks[page_id]

    def request(self, method, path, body=None):
        check_notion_body(body)
        if method == "GET" and path.startswith("/pages/"):
            return next(page for page in self.pages if page["id"] == path.removeprefix("/pages/"))
        self.writes.append((method, path, body))
        if not self.allow_writes:
            raise AssertionError("audit must be read-only")
        if (method, path) == ("POST", "/pages"):
            self.blocks["home"].append({"id": "archive", "type": "child_page",
                                         "child_page": {"title": "旧 Daily・レトプラ記録"}})
            return {"id": "archive"}
        if method == "POST" and path.endswith("/move"):
            if self.fail_move:
                raise NotionError("move failed")
            page_id = path.split("/")[2]
            page = next(page for page in self.pages if page["id"] == page_id)
            page["parent"] = body["parent"]
            return page
        raise AssertionError((method, path, body))


class FakeHub:
    class State:
        home_id = "home"
        daily_ds_id = "daily-ds"

    def __init__(self):
        self.state = self.State()
        self.sections = {}
        self.rows = {}
        self.legacy_ids = {}

    def section_body(self, day, kind):
        return self.sections.get((day, kind), "")

    def upsert_day(self, kind, day, title, markdown, slack_url, file):
        self.sections[(day, kind)] = markdown
        self.rows.setdefault(day, {"id": f"day-{day}"})

    def _day(self, day):
        return self.rows.get(day)

    def day_body(self, day):
        return "\n".join(body for (d, _), body in self.sections.items() if d == day)

    def set_legacy_ids(self, day, ids):
        self.legacy_ids.setdefault(day, set()).update(ids)


class FakeStore:
    def __init__(self):
        self.links = {}
        self.relinked = []

    def relink_notion_pages(self, mapping):
        self.relinked.append(mapping)


def test_audit_preserves_two_reviews_on_same_day_and_all_urls():
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "一つ目")
    notion.note("r2", "2026-09-21", "振り返り", "二つ目")

    manifest = audit_legacy_notes(notion, "notes-ds")

    assert len(manifest.entries) == 2
    assert [entry.page_id for entry in manifest.entries] == ["r1", "r2"]
    assert {entry.url for entry in manifest.entries} == {"https://notion.so/r1", "https://notion.so/r2"}
    assert [entry.body for entry in manifest.entries] == ["一つ目", "二つ目"]
    assert notion.writes == []


def test_copy_verification_rejects_extra_text_inside_legacy_marker():
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "元の本文")
    entry = audit_legacy_notes(notion, "notes-ds").entries[0]
    altered = _entry_text(entry).replace("元の本文\n", "元の本文\n余計な本文\n")
    assert not _copied(altered, entry)


def test_manifest_preflight_can_resume_after_one_original_was_moved():
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "一つ目")
    notion.note("r2", "2026-09-22", "振り返り", "二つ目")
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.pages[0]["parent"] = {"type": "page_id", "page_id": "archive"}
    notion.blocks["home"].append({"id": "archive", "type": "child_page",
                                  "child_page": {"title": "旧 Daily・レトプラ記録"}})

    verify_manifest_current(notion, manifest, "home")


def test_audit_rejects_missing_date_without_writing():
    notion = LegacyNotion()
    notion.note("r1", None, "Daily", "朝の内容")

    with pytest.raises(NotionError, match="日付"):
        audit_legacy_notes(notion, "notes-ds")
    assert notion.writes == []


def test_audit_preserves_generated_bold_text():
    notion = LegacyNotion()
    notion.note("d1", "2026-09-24", "Daily", "今日のタスク")
    notion.blocks["d1"][0]["paragraph"]["rich_text"][0]["annotations"] = {"bold": True}

    manifest = audit_legacy_notes(notion, "notes-ds")

    assert manifest.entries[0].body == "**今日のタスク**"


def test_apply_keeps_two_reviews_and_is_idempotent():
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "一つ目")
    notion.note("r2", "2026-09-21", "振り返り", "二つ目")
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.allow_writes = True
    hub, store = FakeHub(), FakeStore()

    first = apply_legacy_notes(notion, hub, manifest, store)
    second = apply_legacy_notes(notion, hub, manifest, store)

    assert first.copied == 2 and first.moved == 2 and first.unresolved == ()
    assert second.copied == 0 and second.moved == 0 and second.unresolved == ()
    assert len(hub.rows) == 1
    assert hub.day_body("2026-09-21").count("一つ目") == 1
    assert hub.day_body("2026-09-21").count("二つ目") == 1
    assert "https://notion.so/r1" in hub.day_body("2026-09-21")
    assert "https://notion.so/r2" in hub.day_body("2026-09-21")
    assert store.relinked[-1] == {"r1": "day-2026-09-21", "r2": "day-2026-09-21"}
    assert hub.legacy_ids["2026-09-21"] == {"r1", "r2"}


def test_move_failure_keeps_original_and_does_not_relink():
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "一つ目")
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.allow_writes = True
    notion.fail_move = True
    store = FakeStore()

    report = apply_legacy_notes(notion, FakeHub(), manifest, store)

    assert report.unresolved == ("r1",)
    assert notion.pages[0]["parent"]["data_source_id"] == "notes-ds"
    assert store.relinked == []


def test_changed_original_stops_before_copy_or_move():
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "監査した本文")
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.blocks["r1"][0]["paragraph"]["rich_text"] = rich("あとから変わった")
    notion.allow_writes = True
    hub, store = FakeHub(), FakeStore()

    with pytest.raises(NotionError, match="変わりました"):
        apply_legacy_notes(notion, hub, manifest, store)

    assert hub.rows == {}
    assert notion.writes == []
    assert store.relinked == []


def test_relinking_all_legacy_review_threads_is_transactional(tmp_path):
    store = Store(tmp_path / "state.db")
    store.link_notion("C1", "1.1", "r1", "review")
    store.link_notion("C1", "2.2", "r2", "review")

    count = store.relink_notion_pages({"r1": "day-21", "r2": "day-21"})

    assert count == 2
    assert store.notion_link("C1", "1.1")["page_id"] == "day-21"
    assert store.notion_link("C1", "2.2")["page_id"] == "day-21"


def test_old_research_view_is_moved_to_trash_with_current_api():
    from kei_agent.notion_hub_migration import _archive_research_view

    notion = LegacyNotion()
    notion.allow_writes = True
    notion.blocks[RESEARCH_HOME_ID] = [
        {"id": "h", "type": "heading_2", "heading_2": {"rich_text": rich("最近の Daily と振り返り")}},
        {"id": "v", "type": "child_database", "child_database": {"title": "最近の Daily と振り返り"}},
    ]
    patched = []
    original = notion.request

    def request(method, path, body=None):
        if method == "PATCH" and path.startswith("/blocks/"):
            check_notion_body(body)
            patched.append((path, body))
            return {}
        return original(method, path, body)
    notion.request = request
    _archive_research_view(notion)
    assert patched == [("/blocks/v", {"in_trash": True}), ("/blocks/h", {"in_trash": True})]


def test_apply_into_existing_day_does_not_duplicate_managed_marker():
    from datetime import datetime

    from test_notion_hub import FakeDayNotion

    from kei_agent.notion_hub import MANAGED_END, HubState, HubStore

    hub = HubStore(FakeDayNotion(), HubState("home", "calendar-ds", "daily-ds"))
    row = hub.upsert_day("振り返り", "2026-09-21", "Retro", "今の振り返り", None, None)
    hub.append_review_conclusion(row.id, "決めたこと", datetime(2026, 9, 21, 21))
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "旧い振り返り")
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.allow_writes = True

    report = apply_legacy_notes(notion, hub, manifest, FakeStore())

    body = hub.day_body("2026-09-21")
    assert report.unresolved == ()
    assert body.count(MANAGED_END) == 2  # Daily とレトプラに1つずつ
    assert body.count("決めたこと") == 1
    assert body.count("今の振り返り") == 1 and "旧い振り返り" in body
