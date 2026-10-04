"""Kei Agent 本体だけが扱う共通 Notion ホーム。agent 用 gateway とは分離する。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta
from typing import Literal

from kei_agent.configuration.config import notion_id
from kei_agent.storage.notion import (
    BLOCKS_PER_REQUEST,
    GATEWAY_TOKEN_ENV,
    Notion,
    NotionError,
    append_blocks,
    gateway_notion,
)
from kei_agent.storage.notion_store import (
    Note,
    blocks_to_markdown,
    markdown_to_blocks,
    plain_text,
    rich_text,
    summarize,
)

log = logging.getLogger(__name__)


DAILY_PROPERTIES = {
    "日付": {"title": {}},
    "Daily": {"rich_text": {}},
    "レトプラ": {"rich_text": {}},
    "対象日": {"date": {}},
    "Daily Slack": {"url": {}},
    "レトプラ Slack": {"url": {}},
}
CALENDAR_REQUIRED = {"名前": "title", "日付": "date", "タグ": "multi_select"}
CALENDAR_ADDITIONS = {
    "出典": {"select": {"options": [{"name": name} for name in ("Outlook", "課題", "手入力")]}},
    "出典 ID": {"rich_text": {}},
    "元 URL": {"url": {}},
    "最終確認": {"date": {}},
    "同期状態": {"select": {"options": [{"name": name} for name in ("確認済み", "要確認")]}},
    "場所": {"rich_text": {}},
    "元の状態": {"rich_text": {}},
}
MANAGED_END = "— Kei Agent の本文ここまで —"
# 研究・大学・仕事の時間を1つにまとめる DB。記録 ID で1回だけ作る
TIME_TITLE = "時間記録"
# 知識の担当が毎朝読む設定のページ（知識ホームの子ページ）。書き方は COLLECT_NOTE
COLLECT_TITLE = "収集"
COLLECT_INTERESTS = "興味"
COLLECT_SOURCES = "情報源"
COLLECT_NOTE = ("知識の担当が毎朝7時に読む設定。興味は「名前: キーワード、キーワード」、"
                "情報源は「zenn: トピック、…」「qiita: タグ、…」か RSS の URL を1行ずつ。")
COLLECT_DEFAULT = f"""{COLLECT_NOTE}

## {COLLECT_INTERESTS}
- AI・LLM・エージェント: LLM、生成AI、AI エージェント、Claude、OpenAI、Gemini、MCP、RAG
- 電子工作・ロボット: M5Stack、スタックチャン、ESP32、Arduino、Raspberry Pi、電子工作、ロボット、スマートホーム
- Web・アプリ開発: TypeScript、React、Next.js、Python、フロントエンド、個人開発、Cloudflare、Vercel

## {COLLECT_SOURCES}
- zenn: llm, ai, claude, mcp, openai, m5stack, esp32, 電子工作, raspberrypi, arduino, react, nextjs, typescript
- qiita: llm, claude, m5stack, esp32, 電子工作, react, typescript
- https://openai.com/news/rss.xml
- https://huggingface.co/blog/feed.xml
- https://research.google/blog/rss/
- https://blog.google/technology/ai/rss/
- https://deepmind.google/blog/rss.xml
- https://github.blog/feed/
- https://www.microsoft.com/en-us/research/feed/
- https://blog.cloudflare.com/rss/
- https://vercel.com/atom
"""


def parse_collect(blocks: list[dict]) -> tuple[list[dict], list[str]]:
    """「収集」ページの箇条書きを、興味（名前とキーワード）と情報源の行にする。読めない行は飛ばす。"""
    section = ""
    interests: list[dict] = []
    sources: list[str] = []
    for block in blocks:
        kind = block.get("type", "")
        body = block.get(kind) or {}
        if kind in ("heading_1", "heading_2", "heading_3"):
            section = plain_text(body.get("rich_text") or []).strip()
        elif kind in ("bulleted_list_item", "numbered_list_item"):
            items = body.get("rich_text") or []
            text = plain_text(items).strip()
            links = [str(item.get("href") or "") for item in items]
            if section == COLLECT_INTERESTS:
                name, _, rest = text.replace("：", ":").partition(":")
                keywords = [k.strip() for k in re.split(r"[,、，]", rest) if k.strip()]
                if name.strip() and keywords:
                    interests.append({"name": name.strip(), "keywords": keywords})
            elif section == COLLECT_SOURCES and text:
                # リンクに題名を付けて貼られたときは、リンク先を使う
                url = next((link for link in links if link.startswith(("http://", "https://"))), "")
                sources.append(text if text.startswith(("http://", "https://")) or not url else url)
    return interests, sources


TIME_DOMAINS = {"research": "研究", "course": "大学", "work": "仕事"}
TIME_SOURCES = ("Slack", "Toggl")
TIME_PROPERTIES = {
    "名前": {"title": {}},
    "領域": {"select": {"options": [{"name": name} for name in TIME_DOMAINS.values()]}},
    "テーマ": {"rich_text": {}},
    "開始": {"date": {}},
    "分": {"number": {"format": "number"}},
    "メモ": {"rich_text": {}},
    "Slack": {"url": {}},
    "記録 ID": {"rich_text": {}},
    "出典": {"select": {"options": [{"name": name} for name in TIME_SOURCES]}},
}
TIME_CHART_NAME = "週ごとの時間"
# 知識ホーム（共通ホームと同じくワークスペースの直下）の DB。記事（Article）と、振り返りの会話で言語化した学び
# （Learning・Advice・Insight）を1つにまとめる。日付は持たず、ページの作成日時を使う
KNOWLEDGE_HOME_TITLE = "知識ホーム"
KNOWLEDGE_TITLE = "Knowledge"
ARTICLE = "Article"
LEARNING = "Learning"
KNOWLEDGE_TYPES = (ARTICLE, LEARNING, "Advice", "Insight")
UNREAD = "Unread"
KNOWLEDGE_STATUSES = (UNREAD, "Read")
# 振り返りの会話の種類（AI は日本語で返す）→ Type
LEARNING_TYPES = {"学び": LEARNING, "助言": "Advice", "気づき": "Insight"}
# 列はこの順に作り、表の表示もこの順にそろえる（notion_hub_setup.HubSetup._order_view）
KNOWLEDGE_PROPERTIES = {
    "Title": {"title": {}},
    "Type": {"select": {"options": [{"name": name} for name in KNOWLEDGE_TYPES]}},
    "Summary": {"rich_text": {}},
    "Source": {"rich_text": {}},
    "Status": {"select": {"options": [{"name": name} for name in KNOWLEDGE_STATUSES]}},
}


def section_blocks(markdown: str) -> list[dict]:
    """日別記録の区画に入れるブロック。見出し2は区画の区切りなので、本文の見出し1・2は3にそろえる。"""
    blocks = []
    for block in markdown_to_blocks(markdown):
        if block["type"] in ("heading_1", "heading_2"):
            block = {"type": "heading_3", "heading_3": block[block["type"]]}
        blocks.append(block)
    return blocks


@dataclass(frozen=True)
class HubState:
    home_id: str
    calendar_ds_id: str
    daily_ds_id: str
    calendar_db_id: str = ""
    daily_db_id: str = ""
    tasks_ds_id: str = ""
    assignments_ds_id: str = ""
    task_view_id: str = ""
    assignment_view_id: str = ""
    daily_view_id: str = ""
    time_db_id: str = ""
    time_ds_id: str = ""
    time_chart_view_id: str = ""
    knowledge_home_id: str = ""
    knowledge_db_id: str = ""
    knowledge_ds_id: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> HubState:
        """状態ファイルの中身から作る。今は使わない鍵（前の読みもの・学びのノートの ID など）は読み飛ばす。"""
        if not isinstance(raw, dict):
            raise TypeError("共通ホームの状態ファイルの形が違います")
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})


class HubStore:
    def __init__(self, notion: Notion, state: HubState):
        self.notion = notion
        self.state = state

    def schema_problems(self) -> list[str]:
        """本体のハブだけ検査し、研究・授業 agent の接続範囲には入らない。"""
        self.notion.request("GET", f"/pages/{self.state.home_id}")
        problems = []
        for label, ds_id, required in (
            ("日別記録", self.state.daily_ds_id,
             {name: next(iter(spec)) for name, spec in DAILY_PROPERTIES.items()}),
            ("予定カレンダー", self.state.calendar_ds_id,
             {**CALENDAR_REQUIRED,
              **{name: next(iter(spec)) for name, spec in CALENDAR_ADDITIONS.items()}}),
        ):
            props = self.notion.request("GET", f"/data_sources/{ds_id}").get("properties", {})
            for name, kind in required.items():
                if props.get(name, {}).get("type") != kind:
                    problems.append(f"{label} の「{name}」が {kind} ではありません")
        for label, ds_id, spec_of in (("時間記録", self.state.time_ds_id, TIME_PROPERTIES),
                                      (KNOWLEDGE_TITLE, self.state.knowledge_ds_id, KNOWLEDGE_PROPERTIES)):
            if not ds_id:
                continue
            props = self.notion.request("GET", f"/data_sources/{ds_id}").get("properties", {})
            for name, spec in spec_of.items():
                if props.get(name, {}).get("type") != next(iter(spec)):
                    problems.append(f"{label}の「{name}」が {next(iter(spec))} ではありません")
        return problems

    def _day_rows(self, day: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{self.state.daily_ds_id}/query", {
            "filter": {"property": "対象日", "date": {"equals": day}},
            "page_size": 100,
        })

    def _day(self, day: str) -> dict | None:
        rows = self._day_rows(day)
        if len(rows) > 1:
            raise NotionError(f"日別記録の {day} が重複しています。正本を確認してください")
        return rows[0] if rows else None

    @staticmethod
    def _section(blocks: list[dict], title: str) -> tuple[dict, list[dict]]:
        headings = [(i, block) for i, block in enumerate(blocks)
                    if block.get("type") == "heading_2"
                    and plain_text(block["heading_2"].get("rich_text", [])) == title]
        if len(headings) != 1:
            raise NotionError(f"日別記録の「{title}」区画がありません、または重複しています")
        index, heading = headings[0]
        end = next((i for i in range(index + 1, len(blocks))
                    if blocks[i].get("type") == "heading_2"), len(blocks))
        return heading, blocks[index + 1:end]

    @staticmethod
    def _managed_end(blocks: list[dict]) -> int:
        matches = [index for index, block in enumerate(blocks)
                   if block.get("type") == "paragraph"
                   and plain_text(block["paragraph"].get("rich_text", [])) == MANAGED_END]
        if len(matches) != 1:
            raise NotionError("日別記録の本文境界がありません、または重複しています。追記を守るため更新を止めます")
        return matches[0]

    def upsert_day(self, kind: Literal["Daily", "振り返り"], day: str, title: str,
                   markdown: str, slack_url: str | None) -> Note:
        if kind not in ("Daily", "振り返り"):
            raise ValueError(f"未知の記録種類: {kind}")
        date.fromisoformat(day)
        if not markdown.strip():
            raise ValueError("空の記録は保存できません")
        column = "Daily" if kind == "Daily" else "レトプラ"
        existing = self._day(day)
        old_blocks = self.notion.children(existing["id"]) if existing else []
        old_section: list[dict] = []
        heading = None
        if existing:
            heading, old_section = self._section(old_blocks, column)
            self._section(old_blocks, "レトプラ" if column == "Daily" else "Daily")
            managed_end = self._managed_end(old_section)
        summary = summarize(markdown, limit=200)
        properties = {
            column: {"rich_text": rich_text(summary)},
            f"{column} Slack": {"url": slack_url},
        }
        content = section_blocks(markdown)
        if existing is None:
            properties.update({
                "日付": {"title": rich_text(day)},
                "対象日": {"date": {"start": day}},
                ("レトプラ" if column == "Daily" else "Daily"): {"rich_text": []},
            })
            sections = [
                markdown_to_blocks("## Daily"),
                content if column == "Daily" else [],
                markdown_to_blocks(MANAGED_END),
                markdown_to_blocks("## レトプラ"),
                content if column == "レトプラ" else [],
                markdown_to_blocks(MANAGED_END),
            ]
            blocks = [block for section in sections for block in section]
            page = self.notion.request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": self.state.daily_ds_id},
                "properties": properties,
                "children": blocks[:BLOCKS_PER_REQUEST],
            })
            append_blocks(self.notion, page["id"], blocks[BLOCKS_PER_REQUEST:])
        else:
            # 新しいブロックを先に置き、成功後に旧区画だけをゴミ箱へ移す。
            if content:
                append_blocks(self.notion, existing["id"], content, heading["id"])
            for block in old_section[:managed_end]:
                self.notion.request("PATCH", f"/blocks/{block['id']}", {"in_trash": True})
            page = self.notion.request("PATCH", f"/pages/{existing['id']}", {"properties": properties})
        return Note(page["id"], title, kind, day, page.get("url"), markdown)

    def day_body(self, day: str) -> str:
        page = self._day(day)
        if page is None:
            return ""
        return blocks_to_markdown(self.notion.children(page["id"]), self.notion.children)

    def append_review_conclusion(self, page_id: str, text: str, stamp: datetime,
                                 message_id: str | None = None) -> None:
        if not text.strip():
            return
        page = self.notion.request("GET", f"/pages/{page_id}")
        if page.get("parent", {}).get("data_source_id") != self.state.daily_ds_id:
            raise NotionError("振り返りの結論の保存先が日別記録ではありません")
        blocks = self.notion.children(page_id)
        heading, section = self._section(blocks, "レトプラ")
        self._managed_end(section)
        key = message_id or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        marker = f"Slack に貼った結論（{stamp:%m/%d %H:%M}、ID {key}）"
        if any(block.get("type") == "heading_3" and
               plain_text(block["heading_3"].get("rich_text", [])) == marker for block in section):
            return
        append_blocks(self.notion, page_id, section_blocks(f"### {marker}\n{text}"),
                      section[-1]["id"] if section else heading["id"])
        summary = plain_text(page["properties"]["レトプラ"].get("rich_text", []))
        self.notion.request("PATCH", f"/pages/{page_id}", {"properties": {
            "レトプラ": {"rich_text": rich_text(summarize(summary + "\n" + text, limit=200))}}})

    def reviews_edited_since(self, since: datetime) -> list[Note]:
        rows = self.notion.paginate("POST", f"/data_sources/{self.state.daily_ds_id}/query", {
            "filter": {"and": [
                {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since.astimezone().isoformat()}},
                {"property": "レトプラ", "rich_text": {"is_not_empty": True}},
            ]},
            "page_size": 100,
        })
        return [Note(row["id"], plain_text(row["properties"]["日付"]["title"]), "振り返り",
                     row["properties"]["対象日"]["date"]["start"], row.get("url"),
                     self.day_body(row["properties"]["対象日"]["date"]["start"])) for row in rows]

    def section_text(self, day: str, kind: Literal["Daily", "振り返り"]) -> str:
        """その日の区画全体（Kei Agent の本文、貼られた結論、手書きの追記）。境界の行だけ除く。"""
        page = self._day(day)
        if page is None:
            return ""
        _, content = self._section(self.notion.children(page["id"]), "Daily" if kind == "Daily" else "レトプラ")
        kept = [block for block in content
                if not (block.get("type") == "paragraph"
                        and plain_text(block["paragraph"].get("rich_text", [])) == MANAGED_END)]
        return blocks_to_markdown(kept, self.notion.children)

    def review_text(self, day: str) -> str:
        """その日のレトプラ全体（翌朝の Daily の材料）。"""
        return self.section_text(day, "振り返り")

    # 収集（知識ホームの子ページ）

    def collect_settings(self) -> tuple[list[dict], list[str]]:
        """知識ホームの「収集」ページの興味と情報源。"""
        if not self.state.knowledge_home_id:
            raise NotionError(f"{KNOWLEDGE_HOME_TITLE}が未設定です（agents.csv の knowledge の行の notion に書き、"
                              "kei-agent-hub-setup --apply を実行してください）")
        page = next((block for block in self.notion.children(self.state.knowledge_home_id)
                     if block.get("type") == "child_page" and block["child_page"].get("title") == COLLECT_TITLE), None)
        if page is None:
            raise NotionError(f"{KNOWLEDGE_HOME_TITLE}に「{COLLECT_TITLE}」ページがありません"
                              "（kei-agent-hub-setup --apply で作れます）")
        return parse_collect(self.notion.children(page["id"]))

    # Knowledge（知識ホームの DB）

    @property
    def has_knowledge_db(self) -> bool:
        """知識ホームの Knowledge が作ってあるか（kei-agent-hub-setup --apply で作る）。"""
        return bool(self.state.knowledge_ds_id)

    def _knowledge_ds(self) -> str:
        if not self.state.knowledge_ds_id:
            raise NotionError(f"{KNOWLEDGE_TITLE} が未作成です。agents.csv の knowledge の行の notion に"
                              f"{KNOWLEDGE_HOME_TITLE}を書き、kei-agent-hub-setup --apply を実行してください")
        return self.state.knowledge_ds_id

    def find_article(self, url: str) -> str | None:
        """Source が url の Article の行（照合キー）。無ければ None。重なっていれば古いほう。"""
        ds_id = self._knowledge_ds()
        rows = self.notion.paginate("POST", f"/data_sources/{ds_id}/query", {
            "filter": {"and": [
                {"property": "Type", "select": {"equals": ARTICLE}},
                {"property": "Source", "rich_text": {"equals": url.strip()}},
            ]},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": 10,
        })
        return rows[0]["id"] if rows else None

    def add_article(self, item: dict) -> str:
        """記事を Type=Article・Status=Unread で1行入れる。同じ URL の Article があれば作らずにそれを返す。返り値はページ ID。

        item は title・url・summary（要約2文）。ほかの鍵（出どころ・興味など）は書かない。
        """
        url = str(item.get("url") or "").strip()
        if not url:
            raise ValueError("記事の URL がありません")
        found = self.find_article(url)
        if found:
            return found
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self._knowledge_ds()},
            "properties": {
                "Title": {"title": rich_text(str(item.get("title") or "").strip()[:200] or "（題名なし）")},
                "Type": {"select": {"name": ARTICLE}},
                "Summary": {"rich_text": rich_text(str(item.get("summary") or "").strip())},
                "Source": {"rich_text": rich_text(url)},
                "Status": {"select": {"name": UNREAD}},
            }})
        return page["id"]

    def add_learning(self, item: dict, link: str = "") -> tuple[str, str]:
        """振り返りの会話で整理した学びを1件、Knowledge に入れる。返り値は (ページ ID, URL)。

        item は title・kind（学び・助言・気づき。英語の Type でもよい）・source（誰から・どこで）・
        scene（場面）・lesson（学んだこと）・next（次にどう使うか）。Status は付けない。
        本文の最後に、学びが出た Slack のスレッドのリンクを書く。
        """
        ds_id = self._knowledge_ds()
        kind = str(item.get("kind") or "").strip()
        kind = LEARNING_TYPES.get(kind, kind)
        properties = {
            "Title": {"title": rich_text(str(item.get("title") or "").strip()[:200] or "（題なし）")},
            # 知らない種類で選択肢を増やさない（Article も学びには使わない）
            "Type": {"select": {"name": kind if kind in KNOWLEDGE_TYPES[1:] else LEARNING}},
            "Summary": {"rich_text": rich_text(str(item.get("lesson") or "").strip()[:500])},
            "Source": {"rich_text": rich_text(str(item.get("source") or "").strip()[:500])},
        }
        body = "\n\n".join(f"## {heading}\n{str(item.get(key) or '').strip() or '（なし）'}"
                            for heading, key in (("場面", "scene"), ("学んだこと", "lesson"), ("次にどう使うか", "next")))
        children = markdown_to_blocks(body)
        if link:
            children.append({"type": "paragraph", "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": "Slack のスレッド", "link": {"url": link}}}]}})
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "properties": properties, "children": children})
        return page["id"], str(page.get("url") or "")

    def trash_page(self, page_id: str) -> None:
        """ページをゴミ箱に入れる（Notion の画面から戻せる）。"""
        self.notion.request("PATCH", f"/pages/{page_id}", {"in_trash": True})

    # 時間記録

    @property
    def has_time_db(self) -> bool:
        """時間記録が作ってあるか（古い状態ファイルには無い。kei-agent-hub-setup --apply で足す）。"""
        return bool(self.state.time_ds_id)

    def _time_ds(self) -> str:
        if not self.state.time_ds_id:
            raise NotionError("時間記録が未作成です。kei-agent-hub-setup --apply を実行してください")
        return self.state.time_ds_id

    def time_url(self) -> str:
        return f"https://www.notion.so/{self.state.time_db_id.replace('-', '')}" if self.state.time_db_id else ""

    def record_time(self, entry_id: str, domain: str, label: str, started_at: str, minutes: int,
                    memo: str = "", slack_url: str = "", source: str = "Slack") -> str:
        """1件を記録 ID で1回だけ作る。同じ ID があれば中身だけ直す。返り値はページ ID。"""
        ds_id = self._time_ds()
        domain = TIME_DOMAINS.get(domain, domain)
        if domain not in TIME_DOMAINS.values() or source not in TIME_SOURCES:
            raise ValueError(f"未知の領域か出典です: {domain} / {source}")
        if not entry_id or minutes <= 0:
            raise ValueError("記録 ID と正の分を指定してください")
        datetime.fromisoformat(started_at)
        properties = {
            "名前": {"title": rich_text(f"{domain} / {label}".strip())},
            "領域": {"select": {"name": domain}},
            "テーマ": {"rich_text": rich_text(label)},
            "開始": {"date": {"start": started_at}},
            "分": {"number": int(minutes)},
            "メモ": {"rich_text": rich_text(memo or "")},
            "Slack": {"url": slack_url or None},
            "記録 ID": {"rich_text": rich_text(entry_id)},
            "出典": {"select": {"name": source}},
        }
        rows = self.notion.paginate("POST", f"/data_sources/{ds_id}/query", {
            "filter": {"property": "記録 ID", "rich_text": {"equals": entry_id}}, "page_size": 10})
        if len(rows) > 1:
            raise NotionError(f"時間記録の {entry_id} が重複しています。正本を確認してください")
        if rows:
            self.notion.request("PATCH", f"/pages/{rows[0]['id']}", {"properties": properties})
            return rows[0]["id"]
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": properties})
        return page["id"]

    def time_ids_since(self, since: date) -> set[str]:
        """その日以降に始まった記録の記録 ID（取り込みの重複を避ける）。"""
        rows = self.notion.paginate("POST", f"/data_sources/{self._time_ds()}/query", {
            "filter": {"property": "開始", "date": {"on_or_after": since.isoformat()}}, "page_size": 100})
        return {plain_text(row["properties"].get("記録 ID", {}).get("rich_text") or []) for row in rows}

    def time_minutes_by_domain(self, start: date, days: int = 7) -> dict[str, float]:
        """start から days 日ぶんの合計（分）を領域ごとに。"""
        rows = self.notion.paginate("POST", f"/data_sources/{self._time_ds()}/query", {
            "filter": {"and": [
                {"property": "開始", "date": {"on_or_after": start.isoformat()}},
                {"property": "開始", "date": {"before": (start + timedelta(days=days)).isoformat()}},
            ]},
            "page_size": 100,
        })
        totals: dict[str, float] = {}
        for row in rows:
            props = row.get("properties", {})
            domain = (props.get("領域", {}).get("select") or {}).get("name") or "-"
            minutes = props.get("分", {}).get("number") or 0
            totals[domain] = totals.get(domain, 0) + float(minutes)
        return totals

    def calendar_rows(self, source: str, window_start: date, window_end: date) -> list[dict]:
        """その出典の行のうち、日付が範囲内か空のもの。"""
        rows = self.notion.paginate("POST", f"/data_sources/{self.state.calendar_ds_id}/query", {
            "filter": {"and": [
                {"property": "出典", "select": {"equals": source}},
                {"or": [
                    {"property": "日付", "date": {"on_or_after": window_start.isoformat()}},
                    {"property": "日付", "date": {"is_empty": True}},
                ]},
            ]},
            "page_size": 100,
        })
        found = []
        for row in rows:
            day = (row["properties"].get("日付", {}).get("date") or {}).get("start") or ""
            # 入れ子の上限があるので、上端は取ってきてから絞る
            if day and day[:10] > window_end.isoformat():
                continue
            found.append({"id": row["id"],
                          "出典 ID": plain_text(row["properties"].get("出典 ID", {}).get("rich_text") or []),
                          "日付": day,
                          "同期状態": (row["properties"].get("同期状態", {}).get("select") or {}).get("name") or ""})
        return found

    def calendar_upsert(self, source: str, item, checked_at: datetime,
                        existing_id: str | None = None) -> None:
        properties = {
            "名前": {"title": rich_text(item.title)},
            "日付": {"date": {"start": item.start, **({"end": item.end} if item.end else {})}},
            "出典": {"select": {"name": source}},
            "出典 ID": {"rich_text": rich_text(item.source_id)},
            "元 URL": {"url": item.url or None},
            "最終確認": {"date": {"start": checked_at.astimezone().isoformat()}},
            "同期状態": {"select": {"name": "確認済み"}},
            "場所": {"rich_text": rich_text(item.location)},
            "元の状態": {"rich_text": rich_text(item.status)},
        }
        if existing_id:
            self.notion.request("PATCH", f"/pages/{existing_id}", {"properties": properties})
        else:
            self.notion.request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": self.state.calendar_ds_id},
                "properties": properties,
            })

    def calendar_mark_stale(self, row_id: str) -> None:
        self.notion.request("PATCH", f"/pages/{row_id}", {
            "properties": {"同期状態": {"select": {"name": "要確認"}}}})


def load_hub(config, env: dict[str, str] | None = None) -> HubStore | None:
    """設定済みの本体接続だけを使う。研究 Notion DB への代替保存はしない。"""
    env = dict(os.environ) if env is None else env
    if not env.get(GATEWAY_TOKEN_ENV) or not config.hub_state_path.exists():
        log.warning("共通 Notion ホームが未設定です。日別記録への保存は行いません")
        return None
    try:
        raw = json.loads(config.hub_state_path.read_text(encoding="utf-8"))
        state = HubState.from_json(raw)
    except (OSError, ValueError, TypeError) as e:
        log.error("共通 Notion ホームの状態ファイルを読めません: %s", e)
        return None
    if notion_id(state.home_id) != config.notion.hub_home:
        log.error("共通 Notion ホームのページ ID が設定と一致しません")
        return None
    if notion_id(state.knowledge_home_id) != notion_id(config.notion.knowledge_home):
        # 知識ホームを替えたのに setup をやり直していない。古いホームの Knowledge には書かない
        if state.knowledge_ds_id:
            log.warning("知識ホームのページ ID が設定と一致しません。kei-agent-hub-setup --apply を実行するまで "
                        "Knowledge には書きません")
        state = replace(state, knowledge_home_id="", knowledge_db_id="", knowledge_ds_id="")
    return HubStore(gateway_notion("kei-agent", env, config), state)

