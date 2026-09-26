"""The shared hub must locate its existing sources before writing anything."""

import json
from datetime import date, datetime

import pytest
from fakes import check_notion_body

from kei_agent.notion import NotionError
from kei_agent.notion_hub import HubSetup, HubState, HubStore, load_hub
from kei_agent.notion_store import plain_text


def test_corrupt_hub_state_disables_only_hub(config):
    config.hub_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.hub_state_path.write_text("{bad json", encoding="utf-8")
    assert load_hub(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "test-token"}) is None


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


def test_calendar_lookup_filters_by_window_but_keeps_cleared_dates():
    def row(row_id, day):
        return {"id": row_id, "properties": {
            "出典 ID": {"rich_text": [{"plain_text": row_id}]},
            "日付": {"date": {"start": day} if day else None},
            "同期状態": {"select": {"name": "確認済み"}},
        }}

    class CalendarNotion:
        def paginate(self, method, path, body):
            assert body["filter"] == {"and": [
                {"property": "出典", "select": {"equals": "Outlook"}},
                {"or": [
                    {"property": "日付", "date": {"on_or_after": "2026-08-01"}},
                    {"property": "日付", "date": {"is_empty": True}},
                ]},
            ]}
            return [row("old", "2026-09-20"), row("cleared", None), row("far", "2027-01-05T10:00:00+09:00")]

    hub = HubStore(CalendarNotion(), HubState("home", "calendar-ds", "daily-ds"))
    rows = hub.calendar_rows("Outlook", date(2026, 8, 1), date(2026, 11, 30))
    assert [(r["出典 ID"], r["日付"]) for r in rows] == [("old", "2026-09-20"), ("cleared", "")]


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
        self.fail_chart = False

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
            title = body["title"][0]["text"]["content"]
            prefix = {"日別記録": "daily", "時間記録": "time"}[title]
            db_id, ds_id = f"{prefix}-db", f"{prefix}-ds"
            self.blocks["home"].append(self.child_db(db_id, title))
            self.databases[db_id] = {"id": db_id, "parent": {"type": "page_id", "page_id": "home"},
                                     "data_sources": [{"id": ds_id}]}
            self.sources[ds_id] = {"id": ds_id, "properties": {
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
        if method == "POST" and path == "/pages" and body.get("parent", {}).get("page_id") == "home":
            page_id = f"child-page-{len(self.blocks)}"
            title = body["properties"]["title"]["title"][0]["text"]["content"]
            self.blocks.setdefault("home", []).append(
                {"id": page_id, "type": "child_page", "child_page": {"title": title}})
            self.blocks[page_id] = list(body.get("children") or [])
            return {"id": page_id}
        if method == "PATCH" and path.startswith("/views/view-"):
            view = next(view for view in self.views if view["id"] == path.removeprefix("/views/"))
            view.update(body)
            return view
        if method == "POST" and path == "/views" and body.get("type") == "chart":
            if self.fail_chart:
                raise NotionError("chart は未対応")
            view = {"id": f"chart-{len(self.views) + 1}", **body}
            self.views.append(view)
            return view
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
    assert [view["name"] for view in fake_notion.views] == ["研究 Task", "授業課題", "週ごとの時間"]
    visible = [prop["property_id"] for prop in fake_notion.daily_view["configuration"]["properties"]
               if prop["visible"]]
    assert visible == ["日付", "Daily", "レトプラ"]
    for view in fake_notion.views[:2]:
        assert view["create_database"]["parent"]["page_id"] == "home"
        whens = [next(iter(cond["date"])) for cond in view["filter"]["and"][0]["or"]]
        assert whens == ["past_year", "this_week", "next_week"]
        assert view["sorts"][0]["direction"] == "ascending"
    assert fake_notion.databases["tasks-db"]["parent"]["page_id"] == "research-home"
    assert fake_notion.databases["assignments-db"]["parent"]["page_id"] == "course-home"
    assert tmp_path.joinpath("hub.json").exists()


def test_old_this_week_views_are_widened_once(fake_notion, tmp_path):
    """前の絞り込み（締切が今週だけ）の表は、次の setup で一度だけ直す。"""
    hub_setup = setup(fake_notion, tmp_path)
    hub_setup.run()
    for view in fake_notion.views[:2]:
        view["filter"] = {"and": [{"property": "締切", "date": {"this_week": {}}}]}
        view.pop("sorts")
    writes_before = len(fake_notion.writes)

    hub_setup.run()
    patched = len(fake_notion.writes) - writes_before
    hub_setup.run()

    assert patched == 2 and len(fake_notion.writes) == writes_before + 2
    assert all("past_year" in str(view["filter"]) for view in fake_notion.views[:2])


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
        return [b for b in self.blocks.get(page_id, []) if not b.get("in_trash")]

    def request(self, method, path, body=None):
        check_notion_body(body)
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
            before = ((body.get("position") or {}).get("after_block") or {}).get("id")
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
                        block["in_trash"] = body["in_trash"]
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
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "元の振り返り", None)
    day_hub.append_review_conclusion(row.id, "決めたこと", datetime(2026, 9, 24, 21))
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝の内容", None)
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "修正した朝の内容", None)
    body = day_hub.day_body("2026-09-24")
    assert len(day_hub.notion.rows) == 1
    assert "修正した朝の内容" in body
    assert "\n朝の内容\n" not in body
    assert "元の振り返り" in body
    assert "決めたこと" in body


def test_upsert_refuses_duplicate_day_before_write(day_hub):
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝", None)
    day_hub.notion.rows.append({**day_hub.notion.rows[0], "id": "duplicate"})
    before = len(day_hub.notion.writes)
    with pytest.raises(NotionError, match="重複"):
        day_hub.upsert_day("Daily", "2026-09-24", "Daily", "再実行", None)
    assert len(day_hub.notion.writes) == before


def test_summary_is_bounded_and_missing_section_stays_empty(day_hub):
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "a" * 2200, None)
    props = day_hub.notion.rows[0]["properties"]
    assert len(props["Daily"]["rich_text"][0]["text"]["content"]) <= 2000
    assert props["レトプラ"]["rich_text"] == []


def test_review_rerun_keeps_user_conclusion(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "最初の本文", None)
    day_hub.append_review_conclusion(row.id, "自分の結論", datetime(2026, 9, 24, 21))
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "再生成した本文", None)
    body = day_hub.day_body("2026-09-24")
    assert "最初の本文" not in body
    assert body.count("自分の結論") == 1
    assert "再生成した本文" in body


def test_two_distinct_conclusions_in_one_minute_are_both_kept(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "本文", None)
    stamp = datetime(2026, 9, 24, 21, 0)
    day_hub.append_review_conclusion(row.id, "結論その一", stamp, "123.001")
    day_hub.append_review_conclusion(row.id, "結論その二", stamp, "123.002")
    day_hub.append_review_conclusion(row.id, "結論その一", stamp, "123.001")
    body = day_hub.day_body("2026-09-24")
    assert body.count("結論その一") == 1
    assert body.count("結論その二") == 1


def test_review_rerun_preserves_arbitrary_manual_append(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "最初の本文", None)
    day_hub.notion.blocks[row.id].append({"id": "manual", "type": "paragraph",
                                          "paragraph": {"rich_text": [{"plain_text": "手書きの追記"}]}})
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "再生成した本文", None)
    body = day_hub.day_body("2026-09-24")
    assert "最初の本文" not in body
    assert "再生成した本文" in body
    assert "手書きの追記" in body


def test_run_creates_time_db_with_weekly_chart(fake_notion, tmp_path):
    state = setup(fake_notion, tmp_path).run()
    assert (state.time_db_id, state.time_ds_id) == ("time-db", "time-ds")
    props = fake_notion.sources["time-ds"]["properties"]
    assert {name: prop["type"] for name, prop in props.items()} == {
        "名前": "title", "領域": "select", "テーマ": "rich_text", "開始": "date", "分": "number",
        "メモ": "rich_text", "Slack": "url", "記録 ID": "rich_text", "出典": "select"}
    chart = fake_notion.views[-1]
    assert state.time_chart_view_id == chart["id"]
    assert chart["database_id"] == "time-db" and chart["data_source_id"] == "time-ds"
    config = chart["configuration"]
    assert config["chart_type"] == "column"
    assert config["x_axis"] == {"type": "date", "property_id": "開始", "group_by": "week",
                                "sort": {"type": "ascending"}}
    assert config["y_axis"] == {"aggregator": "sum", "property_id": "分"}
    assert config["stack_by"]["property_id"] == "領域"
    assert json.loads((tmp_path / "hub.json").read_text())["time_ds_id"] == "time-ds"


def test_chart_failure_does_not_stop_setup(fake_notion, tmp_path):
    fake_notion.fail_chart = True
    hub_setup = setup(fake_notion, tmp_path)
    state = hub_setup.run()
    assert state.time_ds_id == "time-ds" and state.time_chart_view_id == ""
    assert any("グラフ" in warning for warning in hub_setup.warnings)


class FakeTimeNotion:
    def __init__(self):
        self.rows = []
        self.writes = []

    def paginate(self, method, path, body):
        assert (method, path) == ("POST", "/data_sources/time-ds/query")
        flt = body["filter"]
        if "rich_text" in flt:
            wanted = flt["rich_text"]["equals"]
            return [r for r in self.rows
                    if r["properties"]["記録 ID"]["rich_text"][0]["text"]["content"] == wanted]
        if "and" in flt:
            lo = flt["and"][0]["date"]["on_or_after"]
            hi = flt["and"][1]["date"]["before"]
            return [r for r in self.rows if lo <= r["properties"]["開始"]["date"]["start"][:10] < hi]
        lo = flt["date"]["on_or_after"]
        return [r for r in self.rows if lo <= r["properties"]["開始"]["date"]["start"][:10]]

    def request(self, method, path, body=None):
        check_notion_body(body)
        self.writes.append((method, path, body))
        if (method, path) == ("POST", "/pages"):
            assert body["parent"] == {"type": "data_source_id", "data_source_id": "time-ds"}
            row = {"id": f"t-{len(self.rows) + 1}", "properties": body["properties"]}
            self.rows.append(row)
            return row
        if method == "PATCH" and path.startswith("/pages/"):
            row = next(r for r in self.rows if r["id"] == path.removeprefix("/pages/"))
            row["properties"] = body["properties"]
            return row
        raise AssertionError((method, path, body))


@pytest.fixture
def time_hub():
    return HubStore(FakeTimeNotion(), HubState("home", "calendar-ds", "daily-ds", time_db_id="time-db",
                                               time_ds_id="time-ds"))


def test_record_time_is_idempotent_on_entry_id(time_hub):
    time_hub.record_time("e1", "research", "vlm", "2026-09-21T10:00:00+09:00", 25, "読んだ", "https://s", "Slack")
    time_hub.record_time("e1", "research", "vlm", "2026-09-21T10:00:00+09:00", 30, "読んだ", "https://s", "Slack")
    rows = time_hub.notion.rows
    assert len(rows) == 1
    props = rows[0]["properties"]
    assert props["分"]["number"] == 30
    assert props["領域"]["select"]["name"] == "研究"
    assert props["出典"]["select"]["name"] == "Slack"
    assert props["記録 ID"]["rich_text"][0]["text"]["content"] == "e1"


def test_record_time_rejects_unknown_domain(time_hub):
    with pytest.raises(ValueError):
        time_hub.record_time("e1", "hobby", "x", "2026-09-21T10:00:00+09:00", 5)


def test_record_time_without_time_db_says_so():
    hub = HubStore(FakeTimeNotion(), HubState("home", "calendar-ds", "daily-ds"))
    with pytest.raises(NotionError, match="時間記録"):
        hub.record_time("e1", "work", "定例", "2026-09-21T10:00:00+09:00", 5)


def test_week_minutes_are_summed_per_domain(time_hub):
    time_hub.record_time("a", "research", "vlm", "2026-09-21T10:00:00+09:00", 30)
    time_hub.record_time("b", "course", "DB", "2026-09-22T10:00:00+09:00", 45)
    time_hub.record_time("c", "research", "vlm", "2026-09-23T10:00:00+09:00", 15)
    time_hub.record_time("d", "work", "定例", "2026-09-29T10:00:00+09:00", 60)
    assert time_hub.time_minutes_by_domain(date(2026, 9, 21)) == {"研究": 45, "大学": 45}
    assert time_hub.time_ids_since(date(2026, 9, 23)) == {"c", "d"}
    assert time_hub.time_url() == "https://www.notion.so/timedb"


def test_review_text_includes_conclusions_but_not_boundary(day_hub):
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "元の振り返り", None)
    day_hub.append_review_conclusion(row.id, "決めたこと", datetime(2026, 9, 24, 21))
    text = day_hub.review_text("2026-09-24")
    assert "元の振り返り" in text and "決めたこと" in text
    assert "Kei Agent の本文ここまで" not in text
    assert day_hub.review_text("2026-09-25") == ""


def test_section_text_reads_the_whole_daily_section(day_hub):
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝の内容", None)
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "夜の内容", None)
    assert day_hub.section_text("2026-09-24", "Daily") == "朝の内容"
    assert day_hub.section_text("2026-09-24", "振り返り") == "夜の内容"
    assert day_hub.section_text("2026-09-25", "Daily") == ""


def test_headings_inside_a_section_do_not_break_the_day_row(day_hub):
    """見出し2は区画の区切りなので、本文や貼られた結論の見出しは3にそろえて入れる。"""
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "# Retro\n## 今日やったこと\n- 条件B", None)
    day_hub.append_review_conclusion(row.id, "## 結論\n順番が効く", datetime(2026, 9, 24, 21))
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝", None)

    review = day_hub.review_text("2026-09-24")
    assert "条件B" in review and "順番が効く" in review
    assert "### 今日やったこと" in review and "### 結論" in review
    headings = [plain_text(b["heading_2"]["rich_text"]) for b in day_hub.notion.children(row.id)
                if b["type"] == "heading_2"]
    assert headings == ["Daily", "レトプラ"]


def test_setup_matches_ids_with_and_without_dashes(fake_notion, tmp_path):
    """Notion は ID をハイフン付きで返し、config.toml はハイフンなしで持つ。どちらでも同じページとみなす。"""
    for database in fake_notion.databases.values():
        database["parent"]["page_id"] = "-".join(database["parent"]["page_id"])
    state = setup(fake_notion, tmp_path).run()
    assert state.calendar_ds_id and state.daily_ds_id and state.time_ds_id


def test_day_row_has_no_local_file_columns(day_hub):
    """手元にファイルを残さないので、日別記録にファイルの列は作らず、書きもしない。"""
    from kei_agent.notion_hub import DAILY_PROPERTIES

    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝", "https://slack.example/1")
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "夜", None)

    props = day_hub.notion.rows[0]["properties"]
    assert not any("ファイル" in name for name in (*DAILY_PROPERTIES, *props))
    assert props["Daily Slack"] == {"url": "https://slack.example/1"}


def test_collect_page_is_made_once_and_read_back(fake_notion, tmp_path):
    from kei_agent.notion_hub import COLLECT_TITLE, parse_collect

    hub_setup = setup(fake_notion, tmp_path)
    hub_setup.run()
    hub_setup.run()
    pages = [b for b in fake_notion.blocks["home"]
             if b["type"] == "child_page" and b["child_page"]["title"] == COLLECT_TITLE]
    assert len(pages) == 1
    interests, sources = parse_collect(fake_notion.blocks[pages[0]["id"]])
    assert [i["name"] for i in interests] == ["AI・LLM・エージェント", "電子工作・ロボット", "Web・アプリ開発"]
    assert sources[0].startswith("zenn: llm") and "https://vercel.com/atom" in sources


def test_collect_lines_accept_full_width_colons_and_titled_links():
    from kei_agent.notion_hub import parse_collect

    def bullet(text, href=None):
        return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [
            {"type": "text", "plain_text": text, "href": href, "text": {"content": text}}]}}

    def heading(text):
        return {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "plain_text": text}]}}

    blocks = [heading("興味"), bullet("AI：LLM、RAG"), bullet("名前だけ"),
              heading("情報源"), bullet("OpenAI のブログ", "https://openai.com/news/rss.xml"), bullet("qiita: llm")]
    interests, sources = parse_collect(blocks)
    assert interests == [{"name": "AI", "keywords": ["LLM", "RAG"]}]
    assert sources == ["https://openai.com/news/rss.xml", "qiita: llm"]
