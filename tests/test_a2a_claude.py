"""エージェント共通の provider の動かし方（kei_agent_a2a.claude）の、後始末と返事の読み取り。"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytest.importorskip("a2a", reason="a2a-sdk は agents のグループに入っている（uv run --group agents）")

from kei_agent import runner
from kei_agent.research import FIELDS
from kei_agent_a2a import claude


class _Blocking:
    pid = 4321
    returncode = None

    async def communicate(self, _value):
        await asyncio.Event().wait()

    async def wait(self):
        return -9


async def test_connector_kills_the_process_group_when_cancelled(config, monkeypatch):
    """頼んだ側が取り消したときも、claude と中で動いているものを残さない。"""
    killed = []

    async def create(*_args, **_kwargs):
        return _Blocking()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(os, "killpg", lambda pid, _sig: killed.append(pid))

    task = asyncio.create_task(claude.ask_connector(config, "予定", ("calendar_search",),
                                                    config.agent_plugin_dir("work")))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed == [4321]


def test_json_reply_takes_the_outer_array_even_when_items_hold_arrays():
    text = '```json\n[{"subject": "朝会", "attendees": [{"name": "A"}]}]\n```\n出典 [1, 2]'
    assert claude.json_reply(text) == [{"subject": "朝会", "attendees": [{"name": "A"}]}]


def test_json_reply_reads_a_long_reply_without_trying_every_bracket_pair():
    """括弧の多い長い返事でも、読み取りが遅くならない（各位置から1度だけ読む）。"""
    noise = "[注] " * 3000
    items = [{"subject": f"会議{i}"} for i in range(300)]
    assert claude.json_reply(noise + json.dumps(items, ensure_ascii=False)) == items


def test_json_object_tolerates_code_fences_and_preambles():
    text = 'こちらです。\n```json\n{"complete": true, "items": []}\n```'
    assert claude.json_object(text, "items") == {"complete": True, "items": []}
    with pytest.raises(ValueError):
        claude.json_object("予定はありません", "items")


async def test_calendar_snapshot_reads_a_fenced_reply(config, store, monkeypatch):
    from kei_agent_work import connector

    async def reply(*_args, **_kwargs):
        return '```json\n{"complete": true, "has_more": false, "source_count": 1, "items": [' \
               '{"id": "e1", "subject": "会議", "start": "2026-09-25T11:00", "end": "2026-09-25T12:00"}]}\n```'

    monkeypatch.setattr(connector.claude, "ask_connector", reply)
    snapshot = await connector.calendar_snapshot(config, days=30, store=store)

    assert snapshot["complete"] is True
    assert [item["id"] for item in snapshot["items"]] == ["e1"]


async def test_run_sends_only_the_fields_the_orchestrator_reads(config, monkeypatch):
    """検証前の途中の文（_final_candidate）など、受け取る側が使わない項目は封筒に入れない。"""
    result = runner.RunResult(text="できた")
    if hasattr(result, "_final_candidate"):
        result._final_candidate = "検証前の文"

    async def run_model(*_args, **_kwargs):
        return result

    class _Updater:
        pass

    monkeypatch.setattr(claude.runner, "run_model", run_model)
    ws = type("W", (), {"cwd": config.research_root, "channel_name": "vlm"})()
    payload = json.loads(await claude.run(config, ws, {"prompt": "x"}, _Updater(), recipe=None))

    assert set(payload["data"]) <= set(FIELDS)
    assert "検証前の文" not in json.dumps(payload, ensure_ascii=False)
