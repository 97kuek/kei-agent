"""Slack につながないときの Slack の代わり（conversation/outbox.py）。投稿は頭が読む知らせとしてためる。"""

import pytest
from fakes import make_assistant

from kei_agent.conversation.outbox import Outbox


@pytest.fixture
def outbox(config, store):
    (config.research_root / "vlm").mkdir(parents=True)
    return Outbox(config, store)


async def test_posts_become_notices_with_their_thread(outbox):
    parent = await outbox.chat_postMessage(channel="vlm", text="🧪 ジョブ「評価」を投入したよ")
    await outbox.chat_postMessage(channel="vlm", thread_ts=parent["ts"], markdown_text="🧪 評価が終わったよ")
    first, second = outbox.pending()
    assert (first["channel"], first["text"], first["thread"]) == ("vlm", "🧪 ジョブ「評価」を投入したよ", "")
    assert second["thread_ts"] == parent["ts"] and second["thread"] == "🧪 ジョブ「評価」を投入したよ"


async def test_delivered_notices_come_back_only_when_edited(outbox):
    posted = await outbox.chat_postMessage(channel="kei-agent", text="困りごと1")
    outbox.mark_delivered([n["id"] for n in outbox.pending()])
    assert outbox.pending() == []
    await outbox.chat_update(channel="kei-agent", ts=posted["ts"], text="困りごと1\n困りごと2")
    assert [n["text"] for n in outbox.pending()] == ["困りごと1\n困りごと2"]


async def test_blocks_and_files_keep_their_readable_text(outbox):
    await outbox.chat_postMessage(channel="course", blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                                                                         "text": "📚 課題の新着"}}])
    await outbox.files_upload_v2(channel="vlm", thread_ts="1.2",
                                 file_uploads=[{"filename": "diff.txt", "content": "+ 足した行"}])
    texts = [n["text"] for n in outbox.pending()]
    assert texts[0] == "📚 課題の新着" and texts[1].startswith("📎 diff.txt") and "+ 足した行" in texts[1]


async def test_channels_are_the_known_names(outbox):
    names = {c["name"] for c in (await outbox.conversations_list())["channels"]}
    assert {"overview", "kei-agent", "vlm", "course"} <= names and not any("*" in n for n in names)


async def test_created_projects_are_available_to_local_requests(outbox):
    (outbox.config.module_workspace("work") / "billing").mkdir(parents=True)
    names = {c["name"] for c in (await outbox.conversations_list())["channels"]}
    assert "work-billing" in names


async def test_the_assistant_writes_its_troubles_to_the_outbox(config, store):
    box = Outbox(config, store)
    assistant, _ = make_assistant(config, store)
    assistant.slack = box
    await assistant.notify_trouble("ログインが切れました")
    assert any("ログインが切れました" in n["text"] for n in box.pending())




async def test_output_files_point_the_head_to_read_file(outbox):
    await outbox.files_upload_v2(channel="vlm", thread_ts="1.2",
                                 file_uploads=[{"file": "/x/outputs/結果.md", "filename": "結果.md", "title": "outputs/結果.md"}])
    (notice,) = outbox.pending()
    assert "outputs/結果.md" in notice["text"] and "read_file" in notice["text"]
