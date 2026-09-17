import json

import pytest

from ezra.notion import NotionError
from ezra.notion_store import (
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
            "担当": {"select": {"name": "Ezra"}}, "優先度": {"select": None}, "期日": {"date": None},
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
