import json

import pytest

from kei_agent.storage.notion import (
    LOG_HEADING,
    PAPERS_TITLE,
    PREMISES_HEADING,
    TASKS_TITLE,
    THEME_PAPERS,
    THEME_TASKS,
    THEMES,
    Notion,
    NotionError,
    Setup,
    create_theme_databases,
    schema_problems,
    theme_page_blocks,
)
from kei_agent.testing.fakes import FakeNotionAPI


def client(api: FakeNotionAPI):
    """FakeNotionAPI に、Notion と同じ paginate と children を付ける。"""
    class Client:
        request = staticmethod(api.request)

        def paginate(self, method, path, body=None):
            return Notion.paginate(self, method, path, body)

        def children(self, block_id):
            return Notion.children(self, block_id)

    return Client()


def test_columns_follow_the_spec():
    assert list(THEMES["properties"]) == ["Name", "Status", "Start", "End", "Duration"]
    assert [o["name"] for o in THEMES["properties"]["Status"]["status"]["options"]] == ["In progress", "On hold", "Done"]
    assert "dateBetween" in THEMES["properties"]["Duration"]["formula"]["expression"]
    assert list(THEME_TASKS["properties"]) == ["Title", "Status", "Owner", "Due", "Work & Result", "Slack"]
    assert [o["name"] for o in THEME_TASKS["properties"]["Status"]["status"]["options"]] == [
        "Not started", "Tonight", "Running", "Waiting", "Done"]
    assert [o["name"] for o in THEME_TASKS["properties"]["Owner"]["select"]["options"]] == ["Me", "Kei"]
    assert list(THEME_PAPERS["properties"]) == ["Title", "Summary", "Link", "Status"]
    assert [o["name"] for o in THEME_PAPERS["properties"]["Status"]["select"]["options"]] == ["Unread", "Read", "Use"]


def test_theme_page_blocks_put_premises_before_the_log():
    headings = [b["heading_2"]["rich_text"][0]["text"]["content"] for b in theme_page_blocks() if b["type"] == "heading_2"]
    assert headings == [PREMISES_HEADING, LOG_HEADING]


def test_create_theme_databases_is_idempotent():
    api = FakeNotionAPI()
    page = api.add_page(title="amr-query")
    first = create_theme_databases(client(api), page)
    second = create_theme_databases(client(api), page)
    assert first == second and set(first) == {"tasks", "papers"}
    titles = [b["child_database"]["title"] for b in client(api).children(page) if b["type"] == "child_database"]
    assert titles == [TASKS_TITLE, PAPERS_TITLE]


def test_setup_makes_only_the_theme_database_and_the_strategy_page(tmp_path):
    api = FakeNotionAPI()
    home = api.add_page(title="研究ホーム")
    setup = Setup(client(api), home, tmp_path / "notion.json")
    setup.run()
    state = json.loads((tmp_path / "notion.json").read_text())
    assert set(state["databases"]) == {"themes"}
    kinds = [(b["type"], b.get("child_database", b.get("child_page", {})).get("title"))
             for b in client(api).children(home) if b["type"] in ("child_database", "child_page")]
    assert ("child_database", "テーマ") in kinds and ("child_page", "中長期の方針") in kinds
    assert not any(title in ("Task", "ノート", "先行研究") for _, title in kinds)


def test_schema_problems_reports_a_theme_without_its_task_database(tmp_path):
    api = FakeNotionAPI()
    home = api.add_page(title="研究ホーム")
    setup = Setup(client(api), home, tmp_path / "notion.json")
    setup.run()
    ds = setup.state["databases"]["themes"]["data_source_id"]
    api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": "amr-query"}}]},
                                             "Status": {"status": {"name": "In progress"}}})
    problems = schema_problems(client(api), setup.state)
    assert any("amr-query" in p and TASKS_TITLE in p for p in problems)


def test_two_task_databases_on_one_theme_page_stop_with_the_theme_named():
    api = FakeNotionAPI()
    page = api.add_page(title="amr-query")
    api.add_database(page, TASKS_TITLE, THEME_TASKS["properties"])
    api.add_database(page, TASKS_TITLE, THEME_TASKS["properties"])
    with pytest.raises(NotionError, match=TASKS_TITLE):
        create_theme_databases(client(api), page)
