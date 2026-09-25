"""Notion API への接続そのもの（試し直しと、ブロックの追加）。"""

import http.client
import io
import json

import pytest
from fakes import check_notion_body

from kei_agent import notion as notion_mod
from kei_agent.notion import Notion, NotionError, append_blocks


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(notion_mod.time, "sleep", lambda _: None)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(outcomes):
    calls = []

    def fake(req, timeout):
        calls.append(req)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp(outcome)
    return fake, calls


@pytest.mark.parametrize("error", [
    http.client.IncompleteRead(b"{"),
    ConnectionResetError("reset"),
    TimeoutError("timed out"),
    OSError("network down"),
])
def test_connection_errors_are_retried(monkeypatch, error):
    fake, calls = _urlopen([error, json.dumps({"ok": True}).encode()])
    monkeypatch.setattr(notion_mod.urllib.request, "urlopen", fake)
    assert Notion("t").request("GET", "/pages/x") == {"ok": True}
    assert len(calls) == 2


def test_persistent_connection_error_becomes_notion_error(monkeypatch):
    fake, calls = _urlopen([ConnectionResetError("reset")] * (notion_mod.MAX_RETRIES + 1))
    monkeypatch.setattr(notion_mod.urllib.request, "urlopen", fake)
    with pytest.raises(NotionError, match="/pages/x"):
        Notion("t").request("GET", "/pages/x")
    assert len(calls) == notion_mod.MAX_RETRIES + 1


def test_connection_error_on_a_write_is_not_resent(monkeypatch):
    """ページ作成が Notion 側で済んでいたら、送り直すと二重にできる。"""
    fake, calls = _urlopen([ConnectionResetError("reset"), json.dumps({"ok": True}).encode()])
    monkeypatch.setattr(notion_mod.urllib.request, "urlopen", fake)
    with pytest.raises(NotionError, match="書き込みが済んだか分からない"):
        Notion("t").request("POST", "/pages", {"parent": {}})
    assert len(calls) == 1


def test_connection_error_on_a_query_is_resent(monkeypatch):
    fake, calls = _urlopen([TimeoutError("timed out"), json.dumps({"results": []}).encode()])
    monkeypatch.setattr(notion_mod.urllib.request, "urlopen", fake)
    assert Notion("t").request("POST", "/data_sources/ds/query", {}) == {"results": []}
    assert len(calls) == 2


def test_broken_json_becomes_notion_error(monkeypatch):
    fake, _ = _urlopen([b"<html>oops"])
    monkeypatch.setattr(notion_mod.urllib.request, "urlopen", fake)
    with pytest.raises(NotionError, match="JSON"):
        Notion("t").request("GET", "/pages/x")


class _Recorder:
    def __init__(self):
        self.bodies = []

    def request(self, method, path, body=None):
        check_notion_body(body)
        self.bodies.append(body)
        return {"results": [{"id": f"b{len(self.bodies)}-{i}"} for i in range(len(body["children"]))]}


def test_append_blocks_uses_position_and_keeps_order_across_chunks():
    notion = _Recorder()
    blocks = [{"type": "paragraph"} for _ in range(150)]
    append_blocks(notion, "page", blocks, after="heading")
    assert [len(b["children"]) for b in notion.bodies] == [100, 50]
    assert notion.bodies[0]["position"] == {"type": "after_block", "after_block": {"id": "heading"}}
    # 2回目は、1回目に足した最後のブロックの直後に入れる
    assert notion.bodies[1]["position"]["after_block"]["id"] == "b1-99"


def test_append_blocks_without_after_appends_to_end():
    notion = _Recorder()
    append_blocks(notion, "page", [{"type": "paragraph"}])
    assert "position" not in notion.bodies[0]


def test_legacy_keys_are_rejected_by_fakes():
    with pytest.raises(AssertionError):
        check_notion_body({"after": "x", "children": []})
    with pytest.raises(AssertionError):
        check_notion_body({"archived": True})


def test_state_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "state" / "hub.json"
    notion_mod.write_json_atomic(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}

    def boom(*_, **__):
        raise OSError("disk full")
    monkeypatch.setattr(notion_mod.json, "dump", boom)
    with pytest.raises(OSError):
        notion_mod.write_json_atomic(path, {"a": 2})
    # 書き損じても元の中身が残り、一時ファイルも残らない
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in path.parent.iterdir()] == ["hub.json"]
