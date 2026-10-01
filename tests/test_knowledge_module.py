"""知識のモジュール（modules/knowledge/module.py）。本体の定期処理・リアクション・招待から、窓口（kei_agent.api）を通して動く。"""

import json
import sys
import time

import pytest
from fakes import FakeClaude, FakeHub, FakeNotion, FakePueue, FakeSlack, make_theme

from kei_agent.assistant import Assistant
from kei_agent.execution import runner
from kei_agent.execution.jobs import JobManager
from kei_agent.framework import modules
from kei_agent.scheduling.schedule import Scheduler


@pytest.fixture
def env(config, store, monkeypatch):
    slack = FakeSlack({"C1": "vlm", "C5": "0-overview", "C9": "0-kei-agent", "C40": "4-knowledge"})
    claude = FakeClaude()
    monkeypatch.setattr(runner, "run_model", claude)
    assistant = Assistant(config, store, slack, JobManager(config, store, FakePueue()), "xoxb-test", "UBOT",
                          notion=FakeNotion(), team_url="https://example.slack.com/", hub=FakeHub())
    return Scheduler(config, store, assistant), assistant, slack, claude


def knowledge(assistant):
    """本体が読み込んだ知識のモジュール（class Module）。"""
    return assistant.modules["knowledge"]


def add_post(assistant, channel, ts, day, item):
    """朝の読みもので出した1件の控え（run_schedule("reading") が残すものと同じ形）。"""
    module = knowledge(assistant)
    module.core.records.put("post", f"{channel}:{ts}", {"channel": channel, "ts": ts, "day": day, "item": item,
                                                        "liked_at": None, "page": None}, keep_days=30)


class FakeKnowledgeAgent:
    """知識エージェントの代わり。頼まれた材料を覚えて、決めておいた結果を返す。"""
    base_url = "http://127.0.0.1:8792"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked: list[tuple[str, dict]] = []

    async def stream(self, skill, text="", params=None, on_progress=None):
        from kei_agent import a2a
        self.asked.append((skill, json.loads(text)))
        data = self.replies.pop(0) if self.replies else {"items": []}
        return a2a.TaskResult(state="TASK_STATE_COMPLETED", text=json.dumps(
            {"ok": True, "text": "済", "data": data, "limit_reset_at": None, "cost_usd": None}, ensure_ascii=False))


def test_the_host_and_the_agent_share_the_skill_names():
    """本体側（module.py）が頼む仕事は、担当側（agent.py）の名刺に載っている。名前は同じ skills.py から読む。"""
    pytest.importorskip("a2a", reason="名刺は a2a-sdk で作る")
    from kei_agent_a2a import launch

    spec = modules.builtin()["knowledge"]
    code = sys.modules[modules.load_code(spec).__module__]
    build_card, _ = launch.parts(spec)
    card = build_card("http://127.0.0.1:8792")
    assert card.name == "Kei Agent（知識）" and card.description.startswith("興味のある技術記事")
    assert {code.READING_DIGEST, code.PAPER_DIGEST} < {skill.id for skill in card.skills}


# 先行研究

PAPER = {"id": "arXiv:2609.00001", "title": "Counting with VLMs", "url": "https://arxiv.org/abs/2609.00001",
         "authors": ["A. Author"], "year": "2026", "venue": "", "summary": "数えるときの失敗を分けた。",
         "relation": "条件Bの説明に使える。"}


async def test_literature_goes_through_the_knowledge_agent(env, config, store):
    """キーワードと前提を知識の担当に渡し、選ばれた論文を先行研究 DB とテーマのチャンネルに出す。"""
    scheduler, assistant, slack, claude = env
    make_theme(config, "vlm")
    slack.channels["C2"] = "notes"
    make_theme(config, "notes", keywords=None)
    make_theme(config, "archived")  # Kei Agent のいない（アーカイブした）テーマは見張らない
    agent = FakeKnowledgeAgent({"items": []}, {"items": [PAPER]})
    assistant.agents["knowledge"] = agent
    assistant.notion.papers["arXiv:old"] = {"id": "arXiv:old", "themes": ["vlm"]}

    detail = await scheduler.run_task("literature", "2026-09-18")

    assert detail["themes"] == {"vlm": {"status": "no_new"}, "notes": {"status": "no_keywords"}}
    assert slack.posted() == [] and claude.calls == []     # 研究の担当は動かさない
    skill, payload = agent.asked[0]
    assert skill == "paper-digest" and payload["keywords"] == ["vision language model counting"]
    assert "# テーマ: vlm" in payload["premises"] and payload["known_ids"] == ["arXiv:old"]

    detail = await scheduler.run_task("literature", "2026-09-19")

    post, = slack.posted()
    assert post["channel"] == "C1" and post["text"].startswith("📚 先行研究の新着 9/19（土）")
    assert "Counting with VLMs" in post["text"] and "条件Bの説明に使える" in post["text"]
    assert "<https://arxiv.org/abs/2609.00001>" in post["text"] and post["unfurl_links"] is False
    assert "thread_ts" not in post
    assert assistant.notion.papers["arXiv:2609.00001"]["themes"] == ["vlm"]
    # このスレッドの続きは知識の担当が答える
    assert store.thread_agent("C1", detail["themes"]["vlm"]["thread_ts"]) == "knowledge"


async def test_literature_without_notion_does_not_ask_the_agent(env):
    scheduler, assistant, _, _ = env
    assistant.notion = None
    agent = FakeKnowledgeAgent()
    assistant.agents["knowledge"] = agent

    assert await scheduler.run_task("literature", "2026-09-18") == {"status": "no_notion"}
    assert agent.asked == []


# 朝の読みもの

READING = [{"title": "LLM の話", "url": "https://zenn.dev/x", "source": "Zenn", "interests": ["AI"],
            "summary": "要約", "why": "AI に近い"},
           {"title": "RAG の話", "url": "https://zenn.dev/y", "source": "Zenn", "interests": ["AI"],
            "summary": "要約2", "why": "AI に近い"}]


async def test_reading_posts_one_message_per_article(env, config, store):
    """1記事 = 1投稿（👍 とスレッドが記事ごとになる）。記事は控えておき、案内は最後の1件にだけ付ける。"""
    scheduler, assistant, slack, _ = env
    assistant.hub.collect = ([{"name": "AI", "keywords": ["LLM"]}], ["zenn: llm"])
    agent = FakeKnowledgeAgent({"items": READING, "failed_sources": []})
    assistant.agents["knowledge"] = agent

    detail = await scheduler.run_task("reading", "2026-09-26")

    skill, payload = agent.asked[0]
    assert skill == "reading-digest" and payload["sources"] == ["zenn: llm"] and payload["count"] == 5
    assert payload["liked"] == []
    first, second = slack.posted()
    assert first["channel"] == second["channel"] == "C40" and first["unfurl_links"] is False
    assert first["text"].startswith("📰 1/2 *LLM の話*") and second["text"].startswith("📰 2/2 *RAG の話*")
    # URL は <> で囲む（囲まないと、すぐ後の「（Zenn）」まで Slack が URL にしてしまう）
    assert "<https://zenn.dev/x>（Zenn）" in first["text"]
    hint = "気になった記事に 👍 を付けると"
    assert hint not in first["text"] and hint in second["text"]
    posts = knowledge(assistant).core.records.items("post")
    assert sorted(post["item"]["title"] for post in posts) == ["LLM の話", "RAG の話"]
    assert all(post["day"] == "2026-09-26" and post["liked_at"] is None for post in posts)
    assert all(store.get_thread("C40", post["ts"]) for post in posts)      # スレッドの質問は知識の担当へ
    # 👍 していない控えは、30日たつと毎晩の保守で消える
    row = store.module_record("knowledge", "post", f"C40:{posts[0]['ts']}")
    assert row["expires_at"] == pytest.approx(time.time() + 30 * 86400, abs=60)
    assert detail == {"status": "posted", "count": 2, "channel": "C40", "failed_sources": []}


async def test_a_thumbs_up_saves_the_article_and_guides_the_next_picks(env, config, store):
    """依頼者の 👍 で「読みもの」に入れて 📝 を付ける。外すとゴミ箱へ。次の読みものには好みの参考として渡す。"""
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    event = {"reaction": "+1::skin-tone-2", "user": "UME", "item": {"type": "message", "channel": "C40", "ts": "40.1"}}

    await assistant.on_reaction_added(event)
    await assistant.on_reaction_added({**event, "reaction": "thumbsup"})        # もう入っているので何もしない
    await assistant.on_reaction_added({**event, "user": "USOMEONE"})            # 依頼者の 👍 だけを見る
    await assistant.on_reaction_added({**event, "item": {"type": "message", "channel": "C40", "ts": "99.9"}})

    assert assistant.hub.readings == {"reading-1": {"item": READING[0], "day": "2026-09-26"}}
    assert ("reactions_add", {"channel": "C40", "timestamp": "40.1", "name": "memo"}) in slack.calls
    # 👍 した記事は、次からの参考と、外したときに Notion から消すために残す
    assert store.module_record("knowledge", "post", "C40:40.1")["expires_at"] is None
    assistant.hub.collect = ([{"name": "AI", "keywords": ["LLM"]}], ["zenn: llm"])
    agent = FakeKnowledgeAgent({"items": [], "failed_sources": []})
    assistant.agents["knowledge"] = agent
    await scheduler.run_task("reading", "2026-09-27")
    assert agent.asked[0][1]["liked"] == [{"title": "LLM の話", "source": "Zenn", "interests": ["AI"]}]

    await assistant.on_reaction_removed(event)

    assert assistant.hub.trashed == ["reading-1"] and knowledge(assistant).liked() == []
    assert ("reactions_remove", {"channel": "C40", "timestamp": "40.1", "name": "memo"}) in slack.calls
    assert store.module_record("knowledge", "post", "C40:40.1")["expires_at"] is not None


async def test_a_thumbs_up_without_the_reading_db_is_still_remembered(env, store):
    """「読みもの」がまだ無くても、次からの参考には使う。無いことは1回だけ知らせる。"""
    scheduler, assistant, slack, _ = env
    assistant.hub.has_reading_db = False
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    add_post(assistant, "C40", "40.2", "2026-09-26", READING[1])
    for ts in ("40.1", "40.2"):
        await assistant.on_reaction_added({"reaction": "+1", "user": "UME",
                                           "item": {"type": "message", "channel": "C40", "ts": ts}})
    assert assistant.hub.readings == {} and len(knowledge(assistant).liked()) == 2
    assert sum("読みもの" in text for text in slack.texts()) == 1
    assert not any(name == "reactions_add" and kw["name"] == "memo" for name, kw in slack.calls)


async def test_reading_posts_nothing_without_settings_channel_or_new_articles(env):
    """「収集」が空・チャンネルが無いなら担当に頼まない。新着が無ければ黙る。"""
    scheduler, assistant, slack, _ = env
    agent = FakeKnowledgeAgent({"items": [], "failed_sources": ["Zenn llm"]})
    assistant.agents["knowledge"] = agent
    assert (await scheduler.run_task("reading", "2026-09-26"))["status"] == "no_settings"
    assistant.hub.collect = ([{"name": "AI", "keywords": ["LLM"]}], ["zenn: llm"])
    channel = slack.channels.pop("C40")
    assert (await scheduler.run_task("reading", "2026-09-26"))["status"] == "no_channel"
    assert agent.asked == []
    slack.channels["C40"] = channel
    assert (await scheduler.run_task("reading", "2026-09-26"))["status"] == "no_new"
    assert slack.posted() == []


# 招待

async def test_joining_the_knowledge_channel_is_not_a_theme(env, config):
    """#4-knowledge は研究テーマではない。テーマとして登録せず、作業用のディレクトリも作らない。"""
    scheduler, assistant, slack, claude = env
    await assistant.on_member_joined({"user": "UBOT", "channel": "C40"})
    assert assistant.notion.themes == {}
    assert not (config.research_root / "knowledge").exists()
    text, = slack.texts()
    assert text.startswith("Kei Agent です。このチャンネルの用事は知識エージェントに取り次ぎます。")
    assert "読みものを5件" in text and "CLAUDE.md" not in text
