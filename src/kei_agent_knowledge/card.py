"""知識エージェントの Agent Card（何ができるかを書いた名刺）。形は `kei_agent_a2a.card`。"""

from __future__ import annotations

from a2a.types import AgentCard, AgentSkill

from kei_agent_a2a.card import agent_card

READING_DIGEST = "reading-digest"
PAPER_DIGEST = "paper-digest"
ASK = "ask"


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺。base_url は `http://127.0.0.1:8792` のような、外から見える住所。"""
    return agent_card(
        "Kei Agent（知識）",
        "興味のある技術記事と、研究テーマの論文の新着を集めて絞り、選んだものを要約する。記事や論文の質問に答える。"
        "Notion・Slack・手元のファイル・コマンドは持たない（材料は本体から受け取る）",
        base_url,
        skills=[
            AgentSkill(
                id=READING_DIGEST,
                name="朝の読みもの",
                description="本文の JSON（interests: [{name, keywords}]、sources: [zenn:… / qiita:… / RSS の URL]、"
                            "count）を受け取り、RSS の新着から興味ごとに偏らないよう count 件を選び、本文を読んで"
                            "日本語で要約して返す（data.items: title/url/source/interests/summary/why）",
                tags=["reading"],
            ),
            AgentSkill(
                id=PAPER_DIGEST,
                name="論文の新着",
                description="本文の JSON（theme、keywords、premises、known_ids、count）を受け取り、arXiv の新着から"
                            "前提と関係のあるものだけを最大 count 本選び、要旨から要点と研究との関係を書いて返す"
                            "（data.items: id/title/url/authors/year/venue/summary/relation）",
                tags=["papers", "arxiv"],
            ),
            AgentSkill(
                id=ASK,
                name="記事や論文の質問に答える",
                description="選択済み provider が、記事や論文を Web で読んで答える",
                tags=["reading", "papers"],
                examples=["2番を詳しく", "この記事を要約して https://…", "この論文の手法は何が新しい？"],
            ),
        ],
    )
