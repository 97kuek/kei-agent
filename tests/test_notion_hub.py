"""The shared hub must locate its existing sources before writing anything."""

import json
from datetime import date, datetime

import pytest
from fakes import check_notion_body

from kei_agent.storage.notion import NotionError
from kei_agent.storage.notion_hub import HubState, HubStore, load_hub
from kei_agent.storage.notion_hub_setup import HubSetup
from kei_agent.storage.notion_store import plain_text


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

    from kei_agent.storage.notion_hub import CALENDAR_ADDITIONS, CALENDAR_REQUIRED, DAILY_PROPERTIES
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
            "research-home": [],
            "course-home": [self.child_db("assignments-db", "課題")],
        }
        self.databases = {
            "calendar-db": {"id": "calendar-db", "parent": {"type": "page_id", "page_id": "home"},
                            "data_sources": [{"id": "calendar-ds"}]},
            "assignments-db": {"id": "assignments-db", "parent": {"type": "page_id", "page_id": "course-home"},
                               "data_sources": [{"id": "assignments-ds"}]},
        }
        self.sources = {
            "calendar-ds": self.ds("calendar-ds", {"名前": "title", "日付": "date", "タグ": "multi_select"}),
            "assignments-ds": self.ds("assignments-ds", {"Name": "title", "Due": "date", "Status": "status"}),
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
            prefix = {"日別記録": "daily", "時間記録": "time", "読みもの": "reading", "学びのノート": "learning"}[title]
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


def add_existing_daily(notion, tmp_path, with_state=False, extra_view=False):
    notion.blocks["home"].append(notion.child_db("daily-db", "日別記録"))
    notion.databases["daily-db"] = {
        "id": "daily-db", "parent": {"type": "page_id", "page_id": "home"},
        "data_sources": [{"id": "daily-ds"}]}
    notion.sources["daily-ds"] = notion.ds("daily-ds", {"日付": "title"})
    if extra_view:
        notion.daily_view_ids.append("another-view")
    if with_state:
        state = HubState("home", "calendar-ds", "daily-ds", "calendar-db", "daily-db")
        (tmp_path / "hub.json").write_text(json.dumps(state.__dict__), encoding="utf-8")


@pytest.mark.parametrize(("breaks", "match"), [
    (lambda n, _: n.blocks["home"].append(n.child_db("calendar-duplicate", "今月の予定")), "重複"),
    (lambda n, _: n.pages.pop("home"), "共有|アクセス"),
    (lambda n, _: n.databases["assignments-db"]["parent"].update(page_id="other"), "正本|親"),
    # 状態ファイルのない既存の日別記録に、ビューを二重に作らない
    (lambda n, t: add_existing_daily(n, t), "状態ファイル|既存の日別"),
    (lambda n, t: add_existing_daily(n, t, with_state=True, extra_view=True), "ビュー.*一意"),
], ids=["duplicate-calendar", "no-access", "source-parent", "daily-without-state", "ambiguous-view"])
def test_setup_problems_are_rejected_before_any_write(fake_notion, tmp_path, breaks, match):
    """共通ホームの形がおかしいときは、何か書く前に止める（重複や他人のページを作り変えない）。"""
    breaks(fake_notion, tmp_path)
    with pytest.raises(NotionError, match=match):
        setup(fake_notion, tmp_path).run()
    assert fake_notion.writes == []


def test_run_creates_schema_views_and_databases_only_once(fake_notion, tmp_path):
    """二度目の setup では何も書かない。日別・時間・読みもの・集め方のページは1つずつで、正本の親は動かさない。"""
    from kei_agent.storage.notion_hub import COLLECT_TITLE

    hub_setup = setup(fake_notion, tmp_path)
    first = hub_setup.run()
    writes_after_first = len(fake_notion.writes)
    fake_notion.hide_request_fields = True  # Notion の GET は POST の create_database を返さない
    assert hub_setup.run() == first
    assert len(fake_notion.writes) == writes_after_first
    assert (first.calendar_ds_id, first.daily_ds_id) == ("calendar-ds", "daily-ds")
    titles = [b["child_database"]["title"] for b in fake_notion.blocks["home"] if b["type"] == "child_database"]
    assert titles.count("日別記録") == 1 and titles.count("読みもの") == 1
    assert [b["child_page"]["title"] for b in fake_notion.blocks["home"]
            if b["type"] == "child_page"].count(COLLECT_TITLE) == 1
    assert [view["name"] for view in fake_notion.views] == ["授業課題", "週ごとの時間"]
    visible = [prop["property_id"] for prop in fake_notion.daily_view["configuration"]["properties"]
               if prop["visible"]]
    assert visible == ["日付", "Daily", "レトプラ"]
    for view in fake_notion.views[:1]:
        assert view["create_database"]["parent"]["page_id"] == "home"
        whens = [next(iter(cond["date"])) for cond in view["filter"]["and"][0]["or"]]
        assert whens == ["past_year", "this_week", "next_week"]
        assert view["sorts"][0]["direction"] == "ascending"
    assert (first.tasks_ds_id, first.task_view_id) == ("", "")
    assert fake_notion.databases["assignments-db"]["parent"]["page_id"] == "course-home"

    # 時間記録と週ごとのグラフ
    assert (first.time_db_id, first.time_ds_id) == ("time-db", "time-ds")
    assert {name: prop["type"] for name, prop in fake_notion.sources["time-ds"]["properties"].items()} == {
        "名前": "title", "領域": "select", "テーマ": "rich_text", "開始": "date", "分": "number",
        "メモ": "rich_text", "Slack": "url", "記録 ID": "rich_text", "出典": "select"}
    chart = fake_notion.views[-1]
    assert first.time_chart_view_id == chart["id"]
    assert chart["database_id"] == "time-db" and chart["data_source_id"] == "time-ds"
    config = chart["configuration"]
    assert config["chart_type"] == "column"
    assert config["x_axis"] == {"type": "date", "property_id": "開始", "group_by": "week",
                                "sort": {"type": "ascending"}}
    assert config["y_axis"] == {"aggregator": "sum", "property_id": "分"}
    assert config["stack_by"]["property_id"] == "領域"
    assert json.loads((tmp_path / "hub.json").read_text())["time_ds_id"] == "time-ds"

    # 👍 した記事の入れ先
    assert (first.reading_db_id, first.reading_ds_id) == ("reading-db", "reading-ds")
    assert {name: prop["type"] for name, prop in fake_notion.sources["reading-ds"]["properties"].items()} == {
        "名前": "title", "URL": "url", "出どころ": "select", "興味": "multi_select", "要約": "rich_text",
        "日付": "date", "状態": "select"}

    # 集め方のページは、朝の読みもの（schedule.run_reading）と同じ呼び方で読み返せる
    interests, sources = HubStore(fake_notion, HubState("home", "calendar-ds", "daily-ds")).collect_settings()
    assert [i["name"] for i in interests] == ["AI・LLM・エージェント", "電子工作・ロボット", "Web・アプリ開発"]
    assert sources[0].startswith("zenn: llm") and "https://vercel.com/atom" in sources


def test_old_views_are_widened_once_and_duplicate_views_stop_setup(fake_notion, tmp_path):
    """前の絞り込み（締切が今週だけ）の表は次の setup で一度だけ直す。状態ファイルがあっても重複した表は見つけて止める。"""
    hub_setup = setup(fake_notion, tmp_path)
    hub_setup.run()
    for view in fake_notion.views[:1]:
        view["filter"] = {"and": [{"property": "Due", "date": {"this_week": {}}}]}
        view.pop("sorts")
    writes_before = len(fake_notion.writes)
    hub_setup.run()
    hub_setup.run()
    assert len(fake_notion.writes) == writes_before + 1
    assert all("past_year" in str(view["filter"]) for view in fake_notion.views[:1])

    fake_notion.views.append(dict(fake_notion.views[0], id="duplicate-view"))
    with pytest.raises(NotionError, match="重複"):
        hub_setup.run()
    assert len(fake_notion.writes) == writes_before + 1


def test_the_assignment_view_reads_the_new_course_columns(fake_notion, tmp_path):
    """授業課題の表は、課題の Due と Status（Submitted と Overdue 以外）で絞り、Due の近い順に並べる。"""
    setup(fake_notion, tmp_path).run()

    view = next(view for view in fake_notion.views if view["name"] == "授業課題")
    assert view["filter"]["and"][1:] == [
        {"property": "Status", "status": {"does_not_equal": "Submitted"}},
        {"property": "Status", "status": {"does_not_equal": "Overdue"}},
    ]
    assert view["sorts"] == [{"property": "Due", "direction": "ascending"}]


def test_inspect_reports_existing_sources_without_writes(fake_notion, tmp_path):
    details = setup(fake_notion, tmp_path).inspect()
    assert any("今月の予定" in line for line in details)
    assert any("授業課題" in line for line in details)
    assert not any("研究 Task" in line for line in details)
    assert fake_notion.writes == []


def test_old_state_and_old_research_task_view_do_not_break_setup(fake_notion, tmp_path):
    """研究ホームに Task DB が無くても動く。古い状態ファイルの task 項目と、残っている「研究 Task」ビューには触れない。"""
    hub_setup = setup(fake_notion, tmp_path)
    hub_setup.run()
    old = json.loads((tmp_path / "hub.json").read_text())
    old.update(tasks_ds_id="old-tasks-ds", task_view_id="old-view")
    (tmp_path / "hub.json").write_text(json.dumps(old), encoding="utf-8")
    writes_before = len(fake_notion.writes)
    assert hub_setup.run().tasks_ds_id == ""
    assert hub_setup.inspect()
    assert len(fake_notion.writes) == writes_before


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


def test_daily_and_review_share_one_row_and_reruns_keep_what_the_owner_added(day_hub):
    """Daily と振り返りは同じ日の1行。作り直しても、自分の結論と手書きの追記は消さない。"""
    row = day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "最初の本文", None)
    stamp = datetime(2026, 9, 24, 21)
    # 同じ分に貼られた別の結論は両方残し、同じ結論を二度貼っても1つにする
    day_hub.append_review_conclusion(row.id, "自分の結論", stamp, "123.001")
    day_hub.append_review_conclusion(row.id, "結論その二", stamp, "123.002")
    day_hub.append_review_conclusion(row.id, "自分の結論", stamp, "123.001")
    day_hub.notion.blocks[row.id].append({"id": "manual", "type": "paragraph",
                                          "paragraph": {"rich_text": [{"plain_text": "手書きの追記"}]}})
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝の内容", None)
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "修正した朝の内容", None)
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "再生成した本文", None)
    body = day_hub.day_body("2026-09-24")
    assert len(day_hub.notion.rows) == 1
    assert "修正した朝の内容" in body and "\n朝の内容\n" not in body
    assert "最初の本文" not in body and "再生成した本文" in body
    assert body.count("自分の結論") == 1 and body.count("結論その二") == 1 and "手書きの追記" in body
    # 読み返すときは、区画ごと・結論つきで、本文の区切りの印は出さない
    assert day_hub.section_text("2026-09-24", "Daily") == "修正した朝の内容"
    text = day_hub.review_text("2026-09-24")
    assert "再生成した本文" in text and "自分の結論" in text
    assert "Kei Agent の本文ここまで" not in text
    assert day_hub.review_text("2026-09-25") == "" and day_hub.section_text("2026-09-25", "Daily") == ""


def test_upsert_refuses_duplicate_day_before_write(day_hub):
    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "朝", None)
    day_hub.notion.rows.append({**day_hub.notion.rows[0], "id": "duplicate"})
    before = len(day_hub.notion.writes)
    with pytest.raises(NotionError, match="重複"):
        day_hub.upsert_day("Daily", "2026-09-24", "Daily", "再実行", None)
    assert len(day_hub.notion.writes) == before


def test_likes_are_written_to_the_reading_db():
    """👍 した記事は「気になる」で入る。選択肢にカンマは使えない。"""
    class Pages:
        def __init__(self):
            self.sent = []

        def request(self, method, path, body=None):
            self.sent.append((method, path, body))
            return {"id": "page-1"}

    pages = Pages()
    hub = HubStore(pages, HubState("home", "calendar-ds", "daily-ds", reading_ds_id="reading-ds"))
    assert hub.has_reading_db and not HubStore(pages, HubState("home", "c", "d")).has_reading_db
    page_id = hub.add_reading({"title": "LLM の話", "url": "https://zenn.dev/x", "source": "Zenn, Inc.",
                               "interests": ["AI", "AI"], "summary": "要約"}, "2026-09-26")
    method, path, body = pages.sent[0]
    props = body["properties"]
    assert (page_id, method, path, body["parent"]) == (
        "page-1", "POST", "/pages", {"type": "data_source_id", "data_source_id": "reading-ds"})
    assert plain_text(props["名前"]["title"]) == "LLM の話" and props["URL"] == {"url": "https://zenn.dev/x"}
    assert props["出どころ"] == {"select": {"name": "Zenn、 Inc."}}
    assert props["興味"] == {"multi_select": [{"name": "AI"}]}
    assert props["日付"] == {"date": {"start": "2026-09-26"}} and props["状態"] == {"select": {"name": "気になる"}}
    hub.trash_page("page-1")
    assert pages.sent[-1] == ("PATCH", "/pages/page-1", {"in_trash": True})


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


def test_record_time_rejects_unknown_domain_and_missing_time_db(time_hub):
    with pytest.raises(ValueError):
        time_hub.record_time("e1", "hobby", "x", "2026-09-21T10:00:00+09:00", 5)
    hub = HubStore(FakeTimeNotion(), HubState("home", "calendar-ds", "daily-ds"))
    # has_time_db は値として読む（assistant・schedule は `not hub.has_time_db` で見る）
    assert hub.has_time_db is False and time_hub.has_time_db is True
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


def test_day_row_properties_are_bounded_and_have_no_local_file_columns(day_hub):
    """要約は Notion の上限に収め、まだない区画は空のまま。手元にファイルを残さないので、ファイルの列は作らない。"""
    from kei_agent.storage.notion_hub import DAILY_PROPERTIES

    day_hub.upsert_day("Daily", "2026-09-24", "Daily", "a" * 2200, "https://slack.example/1")
    props = day_hub.notion.rows[0]["properties"]
    assert len(props["Daily"]["rich_text"][0]["text"]["content"]) <= 2000
    assert props["レトプラ"]["rich_text"] == []
    day_hub.upsert_day("振り返り", "2026-09-24", "Retro", "夜", None)
    assert day_hub.section_text("2026-09-24", "振り返り") == "夜"
    assert not any("ファイル" in name for name in (*DAILY_PROPERTIES, *props))
    assert props["Daily Slack"] == {"url": "https://slack.example/1"}


def test_collect_lines_accept_full_width_colons_and_titled_links():
    from kei_agent.storage.notion_hub import parse_collect

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
