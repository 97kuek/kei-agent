"""Legacy Daily/Retro migration is audited before it can mutate either store."""

import pytest
from fakes import check_notion_body

from kei_agent.notion import NotionError
from kei_agent.notion_hub_migration import (
    _copied,
    _entry_text,
    apply_legacy_notes,
    audit_legacy_notes,
    verify_manifest_current,
)
from kei_agent.store import Store

RESEARCH_HOME_ID = "research-home"


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

    def upsert_day(self, kind, day, title, markdown, slack_url):
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

    first = apply_legacy_notes(notion, hub, manifest, store, RESEARCH_HOME_ID)
    second = apply_legacy_notes(notion, hub, manifest, store, RESEARCH_HOME_ID)

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

    report = apply_legacy_notes(notion, FakeHub(), manifest, store, RESEARCH_HOME_ID)

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
        apply_legacy_notes(notion, hub, manifest, store, RESEARCH_HOME_ID)

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
    _archive_research_view(notion, RESEARCH_HOME_ID)
    assert patched == [("/blocks/v", {"in_trash": True}), ("/blocks/h", {"in_trash": True})]


def test_apply_into_existing_day_does_not_duplicate_managed_marker():
    from datetime import datetime

    from test_notion_hub import FakeDayNotion

    from kei_agent.notion_hub import MANAGED_END, HubState, HubStore

    hub = HubStore(FakeDayNotion(), HubState("home", "calendar-ds", "daily-ds"))
    row = hub.upsert_day("振り返り", "2026-09-21", "Retro", "今の振り返り", None)
    hub.append_review_conclusion(row.id, "決めたこと", datetime(2026, 9, 21, 21))
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "旧い振り返り")
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.allow_writes = True

    report = apply_legacy_notes(notion, hub, manifest, FakeStore(), RESEARCH_HOME_ID)

    body = hub.day_body("2026-09-21")
    assert report.unresolved == ()
    assert body.count(MANAGED_END) == 2  # Daily とレトプラに1つずつ
    assert body.count("決めたこと") == 1
    assert body.count("今の振り返り") == 1 and "旧い振り返り" in body


def test_legacy_heading_is_copied_without_breaking_the_day_row():
    """旧ノートの見出し2は日別記録の区切りと同じ形なので、3にそろえて入れても照合が通る。"""
    from test_notion_hub import FakeDayNotion

    from kei_agent.notion_hub import HubState, HubStore

    hub = HubStore(FakeDayNotion(), HubState("home", "calendar-ds", "daily-ds"))
    notion = LegacyNotion()
    notion.note("r1", "2026-09-21", "振り返り", "本文")
    notion.blocks["r1"].insert(0, {"id": "h-r1", "type": "heading_2", "heading_2": {"rich_text": rich("今日やったこと")}})
    manifest = audit_legacy_notes(notion, "notes-ds")
    notion.allow_writes = True

    report = apply_legacy_notes(notion, hub, manifest, FakeStore(), RESEARCH_HOME_ID)

    assert report.unresolved == ()
    review = hub.review_text("2026-09-21")
    assert "### 今日やったこと" in review and "本文" in review


# ローカルの daily/・reviews/ を日別記録へ（--local）

def _local_hub():
    from test_notion_hub import FakeDayNotion

    from kei_agent.notion_hub import HubState, HubStore

    return HubStore(FakeDayNotion(), HubState("home", "calendar-ds", "daily-ds"))


def test_local_notes_are_copied_once_and_the_files_are_kept(tmp_path):
    from kei_agent.notion_hub_migration import copy_local_notes

    hub = _local_hub()
    overview = tmp_path / "overview"
    (overview / "daily").mkdir(parents=True)
    (overview / "reviews").mkdir()
    (overview / "daily" / "2026-09-20.md").write_text("*今日のタスク*\n\nなし\n", encoding="utf-8")
    (overview / "daily" / "2026-09-22.md").write_text("", encoding="utf-8")
    (overview / "reviews" / "2026-09-20.md").write_text(
        "# Retro & Planning 2026-09-20\n\n## 今日やったこと\n\n- 条件B を回した\n", encoding="utf-8")
    (overview / "reviews" / "2026-09-21.md").write_text("前の仕組みの振り返り", encoding="utf-8")
    (overview / "reviews" / "memo.md").write_text("x", encoding="utf-8")
    # 21日は、いまの仕組みがもう別の本文を書いている
    hub.upsert_day("振り返り", "2026-09-21", "Retro", "今の本文", None)
    expected = [("daily/2026-09-20.md", "copied"), ("daily/2026-09-22.md", "problem"),
                ("reviews/2026-09-20.md", "copied"), ("reviews/2026-09-21.md", "appended"),
                ("reviews/memo.md", "problem")]

    dry = copy_local_notes(hub, overview, apply=False)
    assert [(r.path, r.status) for r in dry] == expected
    assert len(hub.notion.rows) == 1  # 確認だけでは書かない

    applied = copy_local_notes(hub, overview, apply=True)
    assert [(r.path, r.status) for r in applied] == expected
    again = copy_local_notes(hub, overview, apply=True)
    assert [(r.path, r.status) for r in again if r.status != "problem"] == [
        ("daily/2026-09-20.md", "present"), ("reviews/2026-09-20.md", "present"),
        ("reviews/2026-09-21.md", "present")]

    assert hub.section_text("2026-09-20", "Daily") == "*今日のタスク*\nなし"
    assert "### 今日やったこと" in hub.review_text("2026-09-20")
    review = hub.review_text("2026-09-21")
    assert "今の本文" in review and "前の仕組みの振り返り" in review and "reviews/2026-09-21.md" in review
    assert review.count("前の仕組みの振り返り") == 1
    assert (overview / "daily" / "2026-09-20.md").exists() and (overview / "reviews" / "2026-09-21.md").exists()


def test_local_notes_already_in_the_hub_are_left_alone(tmp_path):
    """動いている仕組みが同じ文を書いていれば、装飾や空行が違っても写さない。"""
    from kei_agent.notion_hub_migration import copy_local_notes

    hub = _local_hub()
    hub.upsert_day("Daily", "2026-09-24", "Daily", "**今日のタスク**\n~済んだこと~\n1. 問い", None)
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-09-24.md").write_text("*今日のタスク*\n\n~済んだこと~\n\n1. 問い\n", encoding="utf-8")
    writes = len(hub.notion.writes)

    report = copy_local_notes(hub, tmp_path, apply=True)

    assert [(r.path, r.status) for r in report] == [("daily/2026-09-24.md", "present")]
    assert len(hub.notion.writes) == writes


# 旧研究ログ・旧学習ログ・手元の仕事の記録を時間記録へ（--time）

class OldTimeNotion:
    """研究ホームの「研究ログ」と授業ホームの「学習ログ」を読むだけの偽物。書いたら落とす。"""

    def __init__(self):
        self.blocks = {
            "research-home": [{"id": "rl-db", "type": "child_database", "child_database": {"title": "研究ログ"}}],
            "course-home": [{"id": "sl-db", "type": "child_database", "child_database": {"title": "学習ログ"}}],
        }
        self.databases = {"rl-db": {"data_sources": [{"id": "rl-ds"}]}, "sl-db": {"data_sources": [{"id": "sl-ds"}]}}
        self.rows = {"rl-ds": [], "sl-ds": []}

    def children(self, page_id):
        return list(self.blocks.get(page_id, []))

    def request(self, method, path, body=None):
        if method == "GET" and path.startswith("/databases/"):
            return self.databases[path.removeprefix("/databases/")]
        raise AssertionError(f"旧 DB には書かない: {method} {path}")

    def paginate(self, method, path, body):
        assert method == "POST" and path.endswith("/query")
        return list(self.rows[path.split("/")[2]])


def old_row(row_id, entry_id, day, minutes, **props):
    properties = {
        "Kei Agent 記録ID": {"rich_text": rich(entry_id) if entry_id else []},
        "日付": {"date": {"start": day} if day else None},
        "時間（分）": {"number": minutes},
        "メモ": {"rich_text": rich(props.pop("memo", ""))},
        "Slack": {"url": props.pop("slack", None)},
    }
    for name, value in props.items():
        properties[name] = {"title": rich(value)} if name == "タイトル" else {"rich_text": rich(value)}
    return {"id": row_id, "url": f"https://notion.so/{row_id}", "properties": properties}


def _time_hub():
    from test_notion_hub import FakeTimeNotion

    from kei_agent.notion_hub import HubState, HubStore

    return HubStore(FakeTimeNotion(), HubState("home", "calendar-ds", "daily-ds", time_db_id="time-db",
                                               time_ds_id="time-ds"))


def test_old_time_logs_and_local_work_are_copied_once(tmp_path, store):
    from datetime import datetime

    from kei_agent.notion_hub_migration import copy_time, local_work_source, old_time_sources

    notion = OldTimeNotion()
    notion.rows["rl-ds"] = [
        old_row("p1", "e1", "2026-09-21T10:00:00.000+09:00", 25, テーマ="vlm", memo="読んだ", slack="https://s/1"),
        old_row("p2", "", None, None, タイトル="実験 A の要約"),                       # 時間ではない行
        old_row("p3", "e3", "2026-09-22T10:00:00+09:00", 30, テーマ="vlm"),            # もう入っている
    ]
    notion.rows["sl-ds"] = [
        old_row("c1", "e2", "2026-09-22T09:00:00+09:00", 50, タイトル="データベース"),
        old_row("c2", "e4", "2026-09-23T09:00:00+09:00", None, タイトル="データベース"),  # 時間がない
    ]
    work = store.start_time_entry("w1", "UME", "work", "C30", "work", "", "", "仕事 / work",
                                  datetime(2026, 9, 22, 13).timestamp(), "not_required")[0]
    store.finish_time_entry("UME", work["started_at"] + 3000)
    hub = _time_hub()
    hub.record_time("e3", "research", "vlm", "2026-09-22T10:00:00+09:00", 30)
    sources = [*old_time_sources(notion, "research-home", "course-home", tmp_path / "notion-course.json"),
               local_work_source(store)]

    dry = copy_time(hub, sources, apply=False)
    assert [(r.name, r.copied, r.present, r.skipped, len(r.problems)) for r in dry] == [
        ("研究ログ", ("e1",), 1, 1, 0), ("学習ログ", ("e2",), 0, 0, 1), ("手元の仕事の記録", ("w1",), 0, 0, 0)]
    assert len(hub.notion.rows) == 1

    copy_time(hub, sources, apply=True)
    rows = {r["properties"]["記録 ID"]["rich_text"][0]["text"]["content"]: r["properties"] for r in hub.notion.rows}
    assert set(rows) == {"e1", "e2", "e3", "w1"}
    assert (rows["e1"]["領域"]["select"]["name"], rows["e1"]["テーマ"]["rich_text"][0]["text"]["content"],
            rows["e1"]["分"]["number"], rows["e1"]["Slack"]["url"]) == ("研究", "vlm", 25, "https://s/1")
    assert (rows["e2"]["領域"]["select"]["name"], rows["e2"]["テーマ"]["rich_text"][0]["text"]["content"]) == (
        "大学", "データベース")
    assert rows["w1"]["領域"]["select"]["name"] == "仕事" and rows["w1"]["分"]["number"] == 50
    assert all(r["出典"]["select"]["name"] == "Slack" for r in rows.values())
    assert store.time_entry("w1")["notion_state"] == "done"

    again = copy_time(hub, [*old_time_sources(notion, "research-home", "course-home",
                                              tmp_path / "notion-course.json"), local_work_source(store)],
                      apply=True)
    assert [r.copied for r in again] == [(), (), ()]
    assert len(hub.notion.rows) == 4


def test_course_time_log_is_found_from_the_course_state_file(tmp_path):
    import json

    from kei_agent.notion_hub_migration import old_time_sources

    notion = OldTimeNotion()
    notion.blocks["course-home"] = []
    notion.rows["state-ds"] = [old_row("c1", "e2", "2026-09-22T09:00:00+09:00", 50, タイトル="データベース")]
    state = tmp_path / "notion-course.json"
    state.write_text(json.dumps({"databases": {"study_logs": {"data_source_id": "state-ds"}}}), encoding="utf-8")
    notion.blocks["research-home"] = []

    research, course = old_time_sources(notion, "research-home", "course-home", state)

    assert research.rows == [] and "見つかりません" in research.problems[0]
    assert [row.entry_id for row in course.rows] == ["e2"]


def test_cli_local_mode_only_reports_without_apply(config, monkeypatch, capsys):
    import sys

    from kei_agent import notion_hub_migration as migration

    hub = _local_hub()
    (config.overview_dir / "daily").mkdir(parents=True)
    (config.overview_dir / "daily" / "2026-09-20.md").write_text("朝の内容", encoding="utf-8")
    monkeypatch.setattr(migration, "load_config", lambda: config)
    monkeypatch.setattr(migration, "load_hub", lambda cfg: hub)
    monkeypatch.setattr(migration, "gateway_notion", lambda *a, **k: pytest.fail("研究 Notes 用の接続は作らない"))
    monkeypatch.setattr(sys, "argv", ["kei-agent-hub-migrate", "--local"])

    migration.main()

    out = capsys.readouterr().out
    assert "確認だけ" in out and "写す\tdaily/2026-09-20.md" in out and "--apply" in out
    assert hub.notion.rows == []


def test_cli_time_mode_copies_with_apply(config, store, monkeypatch, capsys):
    import sys

    from kei_agent import notion_hub_migration as migration

    hub = _time_hub()
    source = migration.TimeSource("研究ログ", [migration.TimeRow("e1", "research", "vlm",
                                                                 "2026-09-21T10:00:00+09:00", 25)])
    monkeypatch.setattr(migration, "load_config", lambda: config)
    monkeypatch.setattr(migration, "load_hub", lambda cfg: hub)
    monkeypatch.setattr(migration, "old_time_sources", lambda *a: [source])
    monkeypatch.setattr(sys, "argv", ["kei-agent-hub-migrate", "--time", "--apply"])

    migration.main()

    assert "研究ログ: 写した 1 件" in capsys.readouterr().out
    assert len(hub.notion.rows) == 1
