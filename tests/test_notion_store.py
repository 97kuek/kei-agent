import json
from datetime import date

import pytest

from kei_agent.storage.notion import (
    PREMISES_HEADING,
    TASKS_TITLE,
    THEME_TASKS,
    THEMES,
    Notion,
    NotionError,
    create_theme_databases,
)
from kei_agent.storage.notion_store import (
    DONE,
    OWNER_KEI,
    OWNER_ME,
    TONIGHT,
    WAITING,
    NotionStore,
    blocks_to_markdown,
    load_notion,
    markdown_to_blocks,
    parse_slack_permalink,
    summarize,
)
from kei_agent.testing.fakes import FakeNotionAPI


def test_parse_slack_permalink():
    assert parse_slack_permalink("https://x.slack.com/archives/C0C2MDB3P7W/p1789636798229039") == (
        "C0C2MDB3P7W", "1789636798.229039")
    assert parse_slack_permalink("https://x.slack.com/archives/C1/p1789636798229039?thread_ts=1.2&cid=C1")[1] == (
        "1789636798.229039")
    assert parse_slack_permalink("https://example.com") is None
    assert parse_slack_permalink(None) is None


def test_markdown_round_trip():
    md = "## 結果\n\n- 条件A **82%**\n- 条件B `91%`\n1. 次に C\n2. 最後に D\n> 引用\n\n```python\nprint(1)\n```\n---\n本文"
    blocks = markdown_to_blocks(md)
    assert [b["type"] for b in blocks] == [
        "heading_2", "bulleted_list_item", "bulleted_list_item", "numbered_list_item", "numbered_list_item",
        "quote", "code", "divider", "paragraph"]
    bold = blocks[1]["bulleted_list_item"]["rich_text"][1]
    assert bold["text"]["content"] == "82%" and bold["annotations"] == {"bold": True}
    assert blocks_to_markdown(blocks) == "## 結果\n- 条件A 82%\n- 条件B 91%\n1. 次に C\n2. 最後に D\n> 引用\n```\nprint(1)\n```\n---\n本文"


def test_long_text_is_split():
    block, = markdown_to_blocks("あ" * 4500)
    assert [len(t["text"]["content"]) for t in block["paragraph"]["rich_text"]] == [2000, 2000, 500]


def test_summarize():
    assert summarize("## 結果\n\n- **条件C** は 71%\n- `outputs/c.png` を見る") == "結果 / 条件C は 71% / outputs/c.png を見る"
    assert len(summarize("あ" * 500)) == 300
    # 行頭の数字は番号として消さない
    assert summarize("0.85 に改善\n2023年のデータ") == "0.85 に改善 / 2023年のデータ"
    assert summarize("- 箇条書き\n1. 番号つき\n## 見出し") == "箇条書き / 番号つき / 見出し"


class RecordingNotion:
    def __init__(self, responses):
        self.calls = []
        self.responses = responses

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.pop(0) if self.responses else {"results": [], "has_more": False}

    def paginate(self, method, path, body=None):
        return Notion.paginate(self, method, path, body)

    def children(self, block_id):
        return []


def test_load_notion_needs_gateway_token_and_state(config, tmp_path):
    gateway = {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "master"}
    assert load_notion(config, env={}) is None
    assert load_notion(config, env={"NOTION_TOKEN": "ntn_x"}) is None  # 素のトークンでは直接つながない
    assert load_notion(config, env=gateway) is None  # 状態のファイルがない
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "notion.json").write_text(json.dumps({"databases": {}}))
    store = load_notion(config, env=gateway)
    assert isinstance(store, NotionStore)
    assert store.notion.base_url == "http://127.0.0.1:8791/notion/v1"
    assert store.notion.token != "master"


def test_store_requires_setup(tmp_path):
    with pytest.raises(NotionError):
        NotionStore(RecordingNotion([]), tmp_path / "missing.json")


def test_pagination_stops_when_the_cursor_is_empty():
    """has_more が立ったまま next_cursor が空だと、同じページを取り続けてしまう。"""
    notion = RecordingNotion([{"results": [{"id": "a"}], "has_more": True, "next_cursor": None}])
    assert Notion.paginate(notion, "POST", "/x", {}) == [{"id": "a"}]


def test_blocks_to_markdown_reads_nested_blocks():
    """トグルや入れ子の箇条書きの中身が、Daily の材料から落ちないようにする。"""
    blocks = [{"id": "b1", "type": "toggle", "has_children": True,
               "toggle": {"rich_text": [{"plain_text": "考察"}]}}]
    inner = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "中身"}]}}]
    assert blocks_to_markdown(blocks, lambda _id: inner) == "- 考察\n  中身"
    assert blocks_to_markdown(blocks) == "- 考察"


# テーマごとの Task


def _client(api):
    class Client:
        request = staticmethod(api.request)

        def paginate(self, method, path, body=None):
            return Notion.paginate(self, method, path, body)

        def children(self, block_id):
            return Notion.children(self, block_id)

    return Client()


@pytest.fixture
def home(tmp_path):
    """テーマの DB が1つある研究ホームと、それを読む NotionStore。"""
    api = FakeNotionAPI()
    page = api.add_page(title="研究ホーム")
    db, ds = api.add_database(page, "テーマ", THEMES["properties"])
    path = tmp_path / "notion.json"
    path.write_text(json.dumps({"databases": {"themes": {"database_id": db, "data_source_id": ds, "properties": {}}}}))
    return api, ds, NotionStore(_client(api), path)


def _theme(api, ds, name, status="In progress"):
    page = api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": name}}]},
                                                    "Status": {"status": {"name": status}}})
    return page, create_theme_databases(_client(api), page)


def _task(api, tasks_ds, title, status=TONIGHT, owner=OWNER_KEI, due=None, work="作業: 条件Cを回す"):
    props = {"Title": {"title": [{"text": {"content": title}}]}, "Status": {"status": {"name": status}},
             "Owner": {"select": {"name": owner}}, "Work & Result": {"rich_text": [{"text": {"content": work}}]}}
    if due:
        props["Due"] = {"date": {"start": due}}
    return api.add_page(data_source=tasks_ds, properties=props)


def test_tonight_tasks_come_only_from_active_themes(home):
    api, ds, store = home
    _, active = _theme(api, ds, "amr-query")
    _, paused = _theme(api, ds, "vlm", status="On hold")
    _task(api, active["tasks"], "条件C")
    _task(api, active["tasks"], "自分の分", owner=OWNER_ME)
    _task(api, paused["tasks"], "止めたテーマ")
    tasks = store.tonight_tasks(5)
    assert [(t.title, t.theme, t.work) for t in tasks] == [("条件C", "amr-query", "作業: 条件Cを回す")]
    assert store.count_tonight_tasks() == 1


def test_update_task_appends_the_result_to_work_and_result(home):
    api, ds, store = home
    _, dbs = _theme(api, ds, "amr-query")
    task_id = _task(api, dbs["tasks"], "条件C")
    store.update_task(task_id, status=DONE, result="71%")
    page = api.request("GET", f"/pages/{task_id}")
    text = "".join(t["plain_text"] for t in page["properties"]["Work & Result"]["rich_text"])
    assert text == "作業: 条件Cを回す\n結果: 71%"
    assert page["properties"]["Status"]["status"]["name"] == DONE


def test_due_and_waiting_tasks_cross_themes(home):
    api, ds, store = home
    _, a = _theme(api, ds, "amr-query")
    _, b = _theme(api, ds, "vlm")
    _task(api, a["tasks"], "今日", status="Not started", owner=OWNER_ME, due="2026-10-04")
    _task(api, b["tasks"], "待ち", status=WAITING)
    assert [t.title for t in store.tasks_due_on(date(2026, 10, 4))] == ["今日"]
    assert [(t.title, t.theme) for t in store.awaiting_tasks()] == [("待ち", "vlm")]


def test_a_theme_without_a_task_database_is_skipped(home):
    api, ds, store = home
    api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": "作りかけ"}}]},
                                             "Status": {"status": {"name": "In progress"}}})
    _, dbs = _theme(api, ds, "amr-query")
    _task(api, dbs["tasks"], "条件C")
    assert [t.theme for t in store.tonight_tasks(5)] == ["amr-query"]


def test_ensure_theme_uses_the_workspace_name_and_builds_the_page(home):
    api, ds, store = home
    assert store.ensure_theme("1-amr-query") is True
    assert store.ensure_theme("amr-query") is False
    (name, page_id), = store.active_themes()
    assert name == "amr-query"
    kinds = [b["type"] for b in _client(api).children(page_id)]
    assert kinds.count("heading_2") == 2 and kinds.count("child_database") == 2


def test_two_task_databases_on_a_theme_page_stop_reads_with_the_theme_named(home):
    api, ds, store = home
    page, _ = _theme(api, ds, "amr-query")
    api.add_database(page, TASKS_TITLE, THEME_TASKS["properties"])
    with pytest.raises(NotionError, match="amr-query"):
        store.tonight_tasks(5)


def test_ensure_theme_repairs_an_existing_theme_page_without_creating_a_row(home):
    api, ds, store = home
    page = api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": "amr-query"}}]},
                                                    "Status": {"status": {"name": "In progress"}}})
    api.add_block(page, "paragraph", "手で書いたメモ")
    assert store.ensure_theme("amr-query") is False
    assert store.ensure_theme("amr-query") is False
    blocks = _client(api).children(page)
    headings = [b["heading_2"]["rich_text"][0]["plain_text"] for b in blocks if b["type"] == "heading_2"]
    assert headings == [PREMISES_HEADING, "進捗ログ"]
    assert blocks[0]["type"] == "heading_2"
    assert [b["child_database"]["title"] for b in blocks if b["type"] == "child_database"] == ["Task", "先行研究"]
    assert len(store.active_themes()) == 1


# Notion の項目のずれ（起動時の確認）


def test_schema_problems_reports_theme_and_per_theme_database_drift(home):
    """Notion の画面で選択肢や列を変えると、絞り込みが落ちる。それを起動のときに先に知らせる。"""
    api, ds, store = home
    page, dbs = _theme(api, ds, "amr-query")
    assert store.schema_problems() == []

    api.items[api.key(dbs["tasks"])]["properties"] = {
        k: v for k, v in api.items[api.key(dbs["tasks"])]["properties"].items() if k != "Owner"}
    problems = store.schema_problems()
    assert len(problems) == 1 and "Owner" in problems[0] and "amr-query" in problems[0]

    live = api.items[api.key(ds)]["properties"]
    live["Status"] = {**live["Status"], "status": {"options": [{"name": "進行中"}]}}
    assert any("Status" in p for p in store.schema_problems())

    del store.state["databases"]["themes"]
    assert any("themes: notion.json にありません" in p for p in store.schema_problems())


def test_schema_problems_skips_themes_that_are_not_in_progress(home):
    api, ds, store = home
    _theme(api, ds, "done-theme", status="Done")
    api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": "held"}}]},
                                             "Status": {"status": {"name": "On hold"}}})
    assert store.schema_problems() == []


def test_schema_problems_keeps_going_when_one_theme_page_cannot_be_read(home):
    api, ds, store = home
    page, _ = _theme(api, ds, "broken")
    _theme(api, ds, "fine")
    inner = store.notion
    real = inner.children

    def children(block_id):
        if api.key(block_id) == api.key(page):
            raise NotionError("boom")
        return real(block_id)

    inner.children = children
    problems = store.schema_problems()
    assert len(problems) == 1 and "broken" in problems[0] and "boom" in problems[0]


def test_schema_problems_reads_each_theme_page_once(home):
    api, ds, store = home
    _theme(api, ds, "amr-query")
    calls = []
    real = store.notion.children
    store.notion.children = lambda block_id: calls.append(block_id) or real(block_id)
    assert store.schema_problems() == []
    assert len(calls) == 1
