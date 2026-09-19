import json

import pytest

from kei_agent.notion import Notion, NotionError
from kei_agent.notion_store import (
    NotionStore,
    blocks_to_markdown,
    load_notion,
    markdown_to_blocks,
    parse_slack_permalink,
    summarize,
)


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


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "notion.json"
    path.write_text(json.dumps({"databases": {
        k: {"database_id": f"db-{k}", "data_source_id": f"ds-{k}", "properties": {}}
        for k in ("themes", "tasks", "notes", "milestones")}}))
    return path


def test_create_night_task_links_theme(state):
    notion = RecordingNotion([
        {"results": [], "has_more": False},                   # 同じ Slack リンクの Task はない
        {"results": [{"id": "theme-1"}], "has_more": False},  # テーマを探す
        {"id": "task-1", "url": "https://notion.example/task-1", "properties": {
            "タイトル": {"title": [{"plain_text": "条件C"}]}, "状態": {"status": {"name": "今夜やる"}},
            "担当": {"select": {"name": "Kei Agent"}}, "優先度": {"select": None}, "期日": {"date": None},
            "テーマ": {"relation": [{"id": "theme-1"}]}, "Slack": {"url": "https://s/p1"}}},
        {"properties": {"名前": {"title": [{"plain_text": "vlm"}]}}},  # テーマ名を引く
    ])
    store = NotionStore(notion, state)

    task = store.create_night_task("条件C", "vlm", "https://s/p1", "本文")

    method, path, body = notion.calls[2]
    assert (method, path) == ("POST", "/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": "ds-tasks"}
    assert body["properties"]["テーマ"] == {"relation": [{"id": "theme-1"}]}
    assert body["properties"]["状態"] == {"status": {"name": "今夜やる"}}
    assert body["children"][0]["type"] == "paragraph"
    assert task.theme_names == ["vlm"]


def test_load_notion_needs_token_and_state(config, tmp_path):
    assert load_notion(config, env={}) is None
    assert load_notion(config, env={"NOTION_TOKEN": "ntn_x"}) is None  # 状態のファイルがない
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "notion.json").write_text(json.dumps({"databases": {}}))
    assert isinstance(load_notion(config, env={"NOTION_TOKEN": "ntn_x"}), NotionStore)


def test_store_requires_setup(tmp_path):
    with pytest.raises(NotionError):
        NotionStore(RecordingNotion([]), tmp_path / "missing.json")


def test_missing_property_is_reported_as_a_notion_error(state):
    """Notion の画面でプロパティ名を変えると、素の KeyError で黙って止まっていた。"""
    notion = RecordingNotion([
        {"results": [{"id": "t1", "url": "u", "properties": {"タイトル": {"title": []}}}], "has_more": False},
    ])
    with pytest.raises(NotionError, match="状態"):
        NotionStore(notion, state).tonight_tasks(5)


def test_pagination_stops_when_the_cursor_is_empty():
    """has_more が立ったまま next_cursor が空だと、同じページを取り続けてしまう。"""
    notion = RecordingNotion([{"results": [{"id": "a"}], "has_more": True, "next_cursor": None}])
    assert Notion.paginate(notion, "POST", "/x", {}) == [{"id": "a"}]


def test_summarize_keeps_numbers_at_the_start_of_a_line():
    assert summarize("0.85 に改善\n2023年のデータ") == "0.85 に改善 / 2023年のデータ"
    assert summarize("- 箇条書き\n1. 番号つき\n## 見出し") == "箇条書き / 番号つき / 見出し"


def test_blocks_to_markdown_reads_nested_blocks():
    """トグルや入れ子の箇条書きの中身が、Daily の材料から落ちないようにする。"""
    blocks = [{"id": "b1", "type": "toggle", "has_children": True,
               "toggle": {"rich_text": [{"plain_text": "考察"}]}}]
    inner = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "中身"}]}}]
    assert blocks_to_markdown(blocks, lambda _id: inner) == "- 考察\n  中身"
    assert blocks_to_markdown(blocks) == "- 考察"


def test_request_waits_and_retries_when_notion_is_busy(monkeypatch):
    from kei_agent import notion as notion_module

    notion = Notion("ntn_x")
    calls = []

    def send(method, path, body):
        calls.append(path)
        if len(calls) < 3:
            raise notion_module._Retryable("429 rate limited", 0)
        return {"ok": True}

    monkeypatch.setattr(notion, "_send", send)
    monkeypatch.setattr(notion_module.time, "sleep", lambda _s: None)
    assert notion.request("GET", "/x") == {"ok": True}
    assert len(calls) == 3


def test_request_gives_up_after_retrying(monkeypatch):
    from kei_agent import notion as notion_module

    notion = Notion("ntn_x")
    monkeypatch.setattr(notion, "_send", lambda *a: (_ for _ in ()).throw(
        notion_module._Retryable("503 unavailable", 0)))
    monkeypatch.setattr(notion_module.time, "sleep", lambda _s: None)
    with pytest.raises(NotionError, match="503"):
        notion.request("GET", "/x")


# Notion の項目のずれ（起動時の確認）


class _FakeNotionApi:
    """data_sources の GET だけに答える偽物。"""

    def __init__(self, properties: dict):
        self.properties = properties

    def request(self, method, path, body=None):
        return {"properties": self.properties}


def _tasks_state():
    return {"databases": {k: {"data_source_id": f"ds-{k}"} for k in ("themes", "tasks", "notes", "milestones")}}


def _live_from_spec(spec):
    live = {}
    for name, want in spec["properties"].items():
        kind = next(iter(want))
        live[name] = {"type": kind, kind: dict(want[kind])}
    for name in spec.get("relations", {}):
        live[name] = {"type": "relation", "relation": {}}
    return live


def test_schema_problems_is_quiet_when_notion_matches():
    from kei_agent.notion import SPECS, schema_problems

    class _Api:
        def request(self, method, path, body=None):
            key = path.split("/")[2].removeprefix("ds-")
            return {"properties": _live_from_spec(SPECS[key])}

    assert schema_problems(_Api(), _tasks_state()) == []


def test_schema_problems_reports_a_renamed_select_option():
    """Notion の画面で「Kei Agent」を別名にすると、夜間 Task の絞り込みが落ちる。それを先に知らせる。"""
    from kei_agent.notion import SPECS, schema_problems

    class _Api:
        def request(self, method, path, body=None):
            key = path.split("/")[2].removeprefix("ds-")
            live = _live_from_spec(SPECS[key])
            if key == "tasks":
                live["担当"]["select"]["options"] = [{"name": "自分"}, {"name": "Ezra"}]
                del live["優先度"]
            return {"properties": live}

    problems = schema_problems(_Api(), _tasks_state())
    assert problems == ['tasks: 項目「担当」に選択肢 Kei Agent がありません', 'tasks: 項目「優先度」がありません']


def test_schema_problems_reports_a_missing_database():
    from kei_agent.notion import schema_problems

    state = _tasks_state()
    del state["databases"]["notes"]
    assert any("notes: notion.json にありません" in p for p in schema_problems(_FakeNotionApi({}), state))
