"""The shared hub must locate its existing sources before writing anything."""

import json
from datetime import date, datetime

import pytest

from kei_agent.notion import NotionError
from kei_agent.notion_hub import HubSetup, HubState, HubStore, load_hub


def test_corrupt_hub_state_disables_only_hub(config):
    config.hub_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.hub_state_path.write_text("{bad json", encoding="utf-8")
    assert load_hub(config, {"NOTION_TOKEN": "test-token"}) is None


def test_hub_schema_check_reads_only_own_home_and_sources():
    paths = []

    class SchemaNotion:
        def request(self, method, path):
            assert method == "GET"
            paths.append(path)
            if path == "/pages/home":
                return {"id": "home"}
            if path.startswith("/data_sources/"):
                required = DAILY_PROPERTIES if path.endswith("daily-ds") else {
                    **{name: {kind: {}} for name, kind in CALENDAR_REQUIRED.items()},
                    **CALENDAR_ADDITIONS}
                return {"properties": {name: {"type": next(iter(spec)) if isinstance(spec, dict) else spec}
                                       for name, spec in required.items()}}
            raise AssertionError(path)

    from kei_agent.notion_hub import CALENDAR_ADDITIONS, CALENDAR_REQUIRED, DAILY_PROPERTIES
    hub = HubStore(SchemaNotion(), HubState("home", "calendar-ds", "daily-ds"))
    assert hub.schema_problems() == []
    assert "/pages/home" in paths
    assert not any("course" in path or "research" in path for path in paths)


def test_calendar_lookup_includes_old_dates_for_stable_id_matching():
    class CalendarNotion:
        def paginate(self, method, path, body):
            assert body["filter"] == {"property": "出典", "select": {"equals": "Outlook"}}
            return [{"id": "old", "properties": {
                "出典 ID": {"rich_text": [{"plain_text": "event-1"}]},
                "日付": {"date": {"start": "2026-09-20"}},
                "同期状態": {"select": {"name": "確認済み"}},
            }}]

    hub = HubStore(CalendarNotion(), HubState("home", "calendar-ds", "daily-ds"))
    rows = hub.calendar_rows("Outlook", date(2026, 9, 24), date(2026, 10, 23))
    assert rows[0]["出典 ID"] == "event-1"
    assert rows[0]["日付"] == "2026-09-20"


class FakeHubNotion:
    def __init__(self):
        self.writes = []
        self.pages = {
            "home": {"id": "home", "parent": {"type": "workspace"}},
            "research-home": {"id": "research-home", "parent": {"type": "workspace"}},
            "course-home": {"id": "course-home", "parent": {"type": "workspace"}},
        }
        self.blocks = {
            "home": [self.child_db("calendar-db", "今月の予定")],
            "research-home": [self.child_db("tasks-db", "Task")],
            "course-home": [self.child_db("assignments-db", "課題")],
        }
        self.databases = {
            "calendar-db": {"id": "calendar-db", "parent": {"type": "page_id", "page_id": "home"},
                            "data_sources": [{"id": "calendar-ds"}]},
            "tasks-db": {"id": "tasks-db", "parent": {"type": "page_id", "page_id": "research-home"},
                         "data_sources": [{"id": "tasks-ds"}]},
            "assignments-db": {"id": "assignments-db", "parent": {"type": "page_id", "page_id": "course-home"},
                               "data_sources": [{"id": "assignments-ds"}]},
        }
        self.sources = {
            "calendar-ds": self.ds("calendar-ds", {"名前": "title", "日付": "date", "タグ": "multi_select"}),
            "tasks-ds": self.ds("tasks-ds", {"タイトル": "title", "期日": "date", "状態": "status"}),
            "assignments-ds": self.ds("assignments-ds", {"課題": "title", "締切": "date", "状態": "status"}),
        }
        self.views = []
        self.daily_view = {"id": "daily-default-view", "name": "Default view", "type": "table",
                           "configuration": None}
        self.daily_view_ids = ["daily-default-view"]
        self.hide_request_fields = False

    @staticmethod
    def child_db(db_id, title):
        return {"id": db_id, "type": "child_database", "child_database": {"title": title}}

    @staticmethod
    def ds(ds_id, props):
        return {"id": ds_id, "properties": {name: {"id": name, "type": kind} for name, kind in props.items()}}

    def children(self, parent):
        return list(self.blocks.get(parent, []))

    def request(self, method, path, body=None):
        if method == "GET":
            if path.startswith("/pages/"):
                return self.pages[path.removeprefix("/pages/")]
            if path.startswith("/databases/"):
                return self.databases[path.removeprefix("/databases/")]
            if path.startswith("/data_sources/"):
                return self.sources[path.removeprefix("/data_sources/")]
            if path.startswith("/views?"):
                if path == "/views?database_id=daily-db":
                    return {"results": [{"id": view_id} for view_id in self.daily_view_ids]}
                return {"results": list(self.views)}
            if path.startswith("/views/"):
                if path == "/views/daily-default-view":
                    return self.daily_view
                view = next(view for view in self.views if view["id"] == path.removeprefix("/views/"))
                return {k: v for k, v in view.items() if k != "create_database"} if self.hide_request_fields else view
            raise AssertionError(path)
        self.writes.append((method, path, body))
        if method == "POST" and path == "/databases":
            db_id = "daily-db"
            self.blocks["home"].append(self.child_db(db_id, "日別記録"))
            self.databases[db_id] = {"id": db_id, "parent": {"type": "page_id", "page_id": "home"},
                                     "data_sources": [{"id": "daily-ds"}]}
            self.sources["daily-ds"] = {"id": "daily-ds", "properties": {
                name: {"id": name, "type": next(iter(config))}
                for name, config in body["initial_data_source"]["properties"].items()}}
            return self.databases[db_id]
        if method == "PATCH" and path.startswith("/data_sources/"):
            ds_id = path.removeprefix("/data_sources/")
            self.sources[ds_id]["properties"].update({
                name: {"id": name, "type": next(iter(config))}
                for name, config in body["properties"].items()})
            return self.sources[ds_id]
        if method == "PATCH" and path.startswith("/databases/"):
            db_id = path.removeprefix("/databases/")
            for blocks in self.blocks.values():
                for block in blocks:
                    if block.get("id") == db_id:
                        block["child_database"]["title"] = body["title"][0]["text"]["content"]
            return self.databases[db_id]
        if (method, path) == ("PATCH", "/views/daily-default-view"):
            self.daily_view.update(body)
            return self.daily_view
        if method == "POST" and path == "/views":
            linked_id = f"linked-db-{len(self.views) + 1}"
            self.databases[linked_id] = {"id": linked_id,
                                         "parent": {"type": "page_id", "page_id": "home"},
                                         "data_sources": [{"id": body["data_source_id"]}]}
            view = {"id": f"view-{len(self.views) + 1}",
                    "parent": {"type": "database_id", "database_id": linked_id}, **body}
            self.views.append(view)
            return view
        raise AssertionError((method, path, body))


@pytest.fixture
def fake_notion():
    return FakeHubNotion()


def setup(fake_notion, tmp_path):
    return HubSetup(fake_notion, "home", tmp_path / "hub.json",
                    research_home_id="research-home", course_home_id="course-home")


def test_duplicate_calendar_is_rejected_before_any_write(fake_notion, tmp_path):
    fake_notion.blocks["home"].append(fake_notion.child_db("calendar-duplicate", "今月の予定"))
    with pytest.raises(NotionError, match="重複"):
        setup(fake_notion, tmp_path).run()
    assert fake_notion.writes == []


def test_missing_hub_access_is_rejected_before_any_write(fake_notion, tmp_path):
    del fake_notion.pages["home"]
    with pytest.raises(NotionError, match="共有|アクセス"):
        setup(fake_notion, tmp_path).run()
    assert fake_notion.writes == []


def test_existing_daily_without_state_stops_before_creating_duplicate_views(fake_notion, tmp_path):
    fake_notion.blocks["home"].append(fake_notion.child_db("daily-db", "日別記録"))
    fake_notion.databases["daily-db"] = {
        "id": "daily-db", "parent": {"type": "page_id", "page_id": "home"},
        "data_sources": [{"id": "daily-ds"}]}
    fake_notion.sources["daily-ds"] = fake_notion.ds("daily-ds", {"日付": "title"})
    with pytest.raises(NotionError, match="状態ファイル|既存の日別"):
        setup(fake_notion, tmp_path).run()
    assert fake_notion.writes == []


def test_existing_daily_with_ambiguous_view_stops_before_schema_writes(fake_notion, tmp_path):
    fake_notion.blocks["home"].append(fake_notion.child_db("daily-db", "日別記録"))
    fake_notion.databases["daily-db"] = {
        "id": "daily-db", "parent": {"type": "page_id", "page_id": "home"},
        "data_sources": [{"id": "daily-ds"}]}
    fake_notion.sources["daily-ds"] = fake_notion.ds("daily-ds", {"日付": "title"})
    fake_notion.daily_view_ids.append("another-view")
    state = HubState("home", "calendar-ds", "daily-ds", "calendar-db", "daily-db")
    (tmp_path / "hub.json").write_text(json.dumps(state.__dict__), encoding="utf-8")
    with pytest.raises(NotionError, match="ビュー.*一意"):
        setup(fake_notion, tmp_path).run()
    assert fake_notion.writes == []


def test_source_parent_mismatch_is_rejected_before_any_write(fake_notion, tmp_path):
    fake_notion.databases["tasks-db"]["parent"]["page_id"] = "other"
    with pytest.raises(NotionError, match="正本|親"):
        setup(fake_notion, tmp_path).run()
    assert fake_notion.writes == []


def test_run_creates_schema_and_linked_views_only_once(fake_notion, tmp_path):
    hub_setup = setup(fake_notion, tmp_path)
    first = hub_setup.run()
    writes_after_first = len(fake_notion.writes)
    fake_notion.hide_request_fields = True  # Notion の GET は POST の create_database を返さない
    second = hub_setup.run()
    assert first == second
    assert len(fake_notion.writes) == writes_after_first
    assert first.calendar_ds_id == "calendar-ds"
    assert first.daily_ds_id == "daily-ds"
    assert [b["child_database"]["title"] for b in fake_notion.blocks["home"]
            if b["type"] == "child_database"].count("日別記録") == 1
    assert [view["name"] for view in fake_notion.views] == ["研究 Task", "授業課題"]
    visible = [prop["property_id"] for prop in fake_notion.daily_view["configuration"]["properties"]
               if prop["visible"]]
    assert visible == ["日付", "Daily", "レトプラ"]
    for view in fake_notion.views:
        assert view["create_database"]["parent"]["page_id"] == "home"
        assert view["filter"]["and"][0]["date"]["this_week"] == {}
    assert fake_notion.databases["tasks-db"]["parent"]["page_id"] == "research-home"
    assert fake_notion.databases["assignments-db"]["parent"]["page_id"] == "course-home"
    assert tmp_path.joinpath("hub.json").exists()


def test_duplicate_linked_view_is_detected_even_with_saved_state(fake_notion, tmp_path):
    hub_setup = setup(fake_notion, tmp_path)
    hub_setup.run()
    duplicate = dict(fake_notion.views[0], id="duplicate-view")
    fake_notion.views.append(duplicate)
    writes_before = len(fake_notion.writes)
    with pytest.raises(NotionError, match="重複"):
        hub_setup.run()
    assert len(fake_notion.writes) == writes_before


def test_inspect_reports_existing_sources_without_writes(fake_notion, tmp_path):
    details = setup(fake_notion, tmp_path).inspect()
    assert any("今月の予定" in line for line in details)
    assert any("Task" in line for line in details)
    assert fake_notion.writes == []


class FakeDayNotion:
    def __init__(self):
        self.rows = []
        self.blocks = {}
        self.writes = []

    def paginate(self, method, path, body):
        assert (method, path) == ("POST", "/data_sources/daily-ds/query")
        day = body["filter"]["date"]["equals"]
        return [row for row in self.rows if row["properties"]["対象日"]["date"]["start"] == day]

    def children(self, page_id):
        return [b for b in self.blocks.get(page_id, []) if not b.get("archived")]

    def request(self, method, path, body=None):
        self.writes.append((method, path, body))
        if (method, path) == ("POST", "/pages"):
            page = {"id": f"day-{len(self.rows) + 1}", "url": f"https://notion.so/day-{len(self.rows) + 1}",
                    "parent": body["parent"], "properties": body["properties"],
                    "last_edited_time": "2026-09-24T21:00:00+09:00"}
            self.rows.append(page)
            self.blocks[page["id"]] = self._numbered(body.get("children") or [])
            return page
        if method == "GET" and path.startswith("/pages/"):
            return next(r for r in self.rows if r["id"] == path.removeprefix("/pages/"))
        if method == "PATCH" and path.startswith("/pages/"):
            page = next(r for r in self.rows if r["id"] == path.removeprefix("/pages/"))
            page["properties"].update(body["properties"])
            return page
        if method == "PATCH" and path.endswith("/children"):
            page_id = path.split("/")[2]
            added = self._numbered(body["children"])
            before = body.get("after")
            if before:
                index = next(i for i, block in enumerate(self.blocks[page_id]) if block["id"] == before)
                self.blocks[page_id][index + 1:index + 1] = added
            else:
                self.blocks[page_id].extend(added)
            return {"results": added}
        if method == "PATCH" and path.startswith("/blocks/"):
            block_id = path.removeprefix("/blocks/")
            for blocks in self.blocks.values():
                for block in blocks:
                    if block["id"] == block_id:
                        block["archived"] = body["archived"]
                        return block
        raise AssertionError((method, path, body))

    def _numbered(self, blocks):
        return [{**b, "id": f"block-{sum(map(len, self.blocks.values())) + i + len(self.writes)}"}
                for i, b in enumerate(blocks)]


@pytest.fixture
def day_hub():
    notion = FakeDayNotion()
    return HubStore(notion, HubState("home", "calendar-ds", "daily-ds"))


def test_daily_and_review_share_one_row_and_keep_conclusion(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "元の振り返り", None, None)
    day_hub.append_review_conclusion(row.id, "決めたこと", datetime(2026, 9, 24, 21))
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝の内容", None, None)
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "修正した朝の内容", None, None)
    body = day_hub.day_body("2026-09-24")
    assert len(day_hub.notion.rows) == 1
    assert "修正した朝の内容" in body
    assert "\n朝の内容\n" not in body
    assert "元の振り返り" in body
    assert "決めたこと" in body


def test_upsert_refuses_duplicate_day_before_write(day_hub):
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝", None, None)
    day_hub.notion.rows.append({**day_hub.notion.rows[0], "id": "duplicate"})
    before = len(day_hub.notion.writes)
    with pytest.raises(NotionError, match="重複"):
        day_hub.upsert_day("Daily", "2026-09-24", "Daily", "再実行", None, None)
    assert len(day_hub.notion.writes) == before


def test_summary_is_bounded_and_missing_section_stays_empty(day_hub):
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "a" * 2200, None, None)
    props = day_hub.notion.rows[0]["properties"]
    assert len(props["Daily"]["rich_text"][0]["text"]["content"]) <= 2000
    assert props["レトプラ"]["rich_text"] == []


def test_review_rerun_keeps_user_conclusion(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "最初の本文", None, None)
    day_hub.append_review_conclusion(row.id, "自分の結論", datetime(2026, 9, 24, 21))
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "再生成した本文", None, None)
    body = day_hub.day_body("2026-09-24")
    assert "最初の本文" not in body
    assert body.count("自分の結論") == 1
    assert "再生成した本文" in body


def test_two_distinct_conclusions_in_one_minute_are_both_kept(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "本文", None, None)
    stamp = datetime(2026, 9, 24, 21, 0)
    day_hub.append_review_conclusion(row.id, "結論その一", stamp, "123.001")
    day_hub.append_review_conclusion(row.id, "結論その二", stamp, "123.002")
    day_hub.append_review_conclusion(row.id, "結論その一", stamp, "123.001")
    body = day_hub.day_body("2026-09-24")
    assert body.count("結論その一") == 1
    assert body.count("結論その二") == 1


def test_review_rerun_preserves_arbitrary_manual_append(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "最初の本文", None, None)
    day_hub.notion.blocks[row.id].append({"id": "manual", "type": "paragraph",
                                          "paragraph": {"rich_text": [{"plain_text": "手書きの追記"}]}})
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "再生成した本文", None, None)
    body = day_hub.day_body("2026-09-24")
    assert "最初の本文" not in body
    assert "再生成した本文" in body
    assert "手書きの追記" in body


def test_legacy_ids_are_recorded_without_replacing_prior_ids(day_hub):
    day_hub.upsert_day("Daily", "2026-09-21", "Daily", "朝", None, None)
    day_hub.set_legacy_ids("2026-09-21", ["old-daily"])
    day_hub.set_legacy_ids("2026-09-21", ["old-review-1", "old-review-2"])
    stored = day_hub.notion.rows[0]["properties"]["移行元 ID"]["rich_text"]
    assert set(json.loads(stored[0]["text"]["content"])) == {
        "old-daily", "old-review-1", "old-review-2"}
