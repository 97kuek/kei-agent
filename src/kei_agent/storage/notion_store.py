"""Kei Agent が動いている間に Notion の研究ホームを読み書きする。

データベースの ID は、kei-agent-notion-setup が書いた ~/.local/state/kei-agent/notion.json から読む。
Claude（claude -p）には Notion を直接触らせず、ここで取ってきたものをファイルにして渡す。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from kei_agent.configuration.config import Config
from kei_agent.storage.notion import (
    BLOCKS_PER_REQUEST,
    PREMISES_HEADING,
    TASKS_TITLE,
    Notion,
    NotionError,
    append_blocks,
    create_theme_databases,
    gateway_notion,
    schema_problems,
    theme_page_blocks,
)

log = logging.getLogger(__name__)

# Notion のテキストの上限（1つの rich_text あたり）
TEXT_LIMIT = 2000
# 1つの段落に入れられる rich_text の要素数
RICH_TEXT_ITEMS = 100
# 入れ子のブロックをたどる深さ
MAX_BLOCK_DEPTH = 3
RESULT_LIMIT = 300

_PERMALINK = re.compile(r"/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d{10})(?P<frac>\d{6})")


NOT_STARTED, TONIGHT, RUNNING, WAITING, DONE = "Not started", "Tonight", "Running", "Waiting", "Done"
OWNER_ME, OWNER_KEI = "Me", "Kei"
THEME_ACTIVE, THEME_ON_HOLD = "In progress", "On hold"
RESULT_PREFIX = "結果: "

_NUMBERED = re.compile(r"^\d+-")


def workspace_name(channel_name: str) -> str:
    """チャンネル名（1-amr-query）から作業場の名前（amr-query）を取る。"""
    return _NUMBERED.sub("", channel_name.lstrip("#"))


@dataclass
class Task:
    id: str
    title: str
    status: str
    owner: str | None
    due: str | None
    work: str
    slack_url: str | None
    theme: str | None
    url: str | None = None


@dataclass
class Note:
    """Daily・振り返りの1行。notion_hub.HubStore が使うので、Hub が整理されるまで残す。"""
    id: str
    title: str
    kind: str | None
    day: str | None
    url: str | None
    body: str = ""


def parse_slack_permalink(url: str | None) -> tuple[str, str] | None:
    """Slack のメッセージのリンクから (チャンネルID, ts) を取り出す。"""
    if not url:
        return None
    m = _PERMALINK.search(url)
    if not m:
        return None
    return m.group("channel"), f"{m.group('ts')}.{m.group('frac')}"


# Markdown とブロックの変換（Daily や Task の本文に使う、よく出てくる形だけ）

def rich_text(text: str) -> list[dict]:
    """**太字** と `コード` だけを装飾に変え、上限ごとに分ける。"""
    parts = []
    for token in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not token:
            continue
        annotations = {}
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            token, annotations = token[2:-2], {"bold": True}
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            token, annotations = token[1:-1], {"code": True}
        for i in range(0, len(token), TEXT_LIMIT):
            item = {"type": "text", "text": {"content": token[i:i + TEXT_LIMIT]}}
            if annotations:
                item["annotations"] = annotations
            parts.append(item)
    return parts[:RICH_TEXT_ITEMS]


def _block(kind: str, text: str, **extra) -> dict:
    return {"type": kind, kind: {"rich_text": rich_text(text), **extra}}


def markdown_to_blocks(markdown: str) -> list[dict]:
    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            language = stripped[3:].strip() or "plain text"
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            text = "\n".join(body)
            blocks.append({"type": "code", "code": {
                "rich_text": [{"type": "text", "text": {"content": text[j:j + TEXT_LIMIT]}}
                              for j in range(0, max(len(text), 1), TEXT_LIMIT)],
                "language": language if language in ("python", "bash", "json", "markdown") else "plain text",
            }})
        elif not stripped:
            pass
        elif m := re.match(r"^(#{1,3})\s+(.*)$", stripped):
            level = min(len(m.group(1)), 3)
            blocks.append(_block(f"heading_{level}", m.group(2)))
        elif m := re.match(r"^[-*]\s+\[( |x)\]\s+(.*)$", stripped):
            blocks.append(_block("to_do", m.group(2), checked=m.group(1) == "x"))
        elif m := re.match(r"^[-*•]\s+(.*)$", stripped):
            blocks.append(_block("bulleted_list_item", m.group(1)))
        elif m := re.match(r"^\d+[.)]\s+(.*)$", stripped):
            blocks.append(_block("numbered_list_item", m.group(1)))
        elif stripped.startswith(">"):
            blocks.append(_block("quote", stripped.lstrip("> ")))
        elif set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            blocks.append({"type": "divider", "divider": {}})
        else:
            blocks.append(_block("paragraph", stripped))
        i += 1
    return blocks


def _prop(props: dict, name: str) -> dict:
    """ページのプロパティを1つ取り出す。

    Notion の画面でプロパティの名前を変えると、ここで気づける。素の KeyError だと
    NotionError ではないので、夜間の処理が Slack に何も出さないまま止まってしまう。
    """
    try:
        return props[name]
    except KeyError:
        raise NotionError(
            f"Notion のプロパティ「{name}」が見つかりません。docs/architecture.md の「Notion」 と照らして直してください"
        ) from None


def plain_text(items: list[dict]) -> str:
    return "".join(t.get("plain_text") or t.get("text", {}).get("content", "") for t in items)


def blocks_to_markdown(blocks: list[dict], children: Callable[[str], list[dict]] | None = None,
                       depth: int = 0) -> str:
    """ブロックを Markdown にする。`children` を渡すと、トグルや入れ子の箇条書きの中も読む。"""
    lines = []
    number = 0
    for b in blocks:
        kind = b.get("type")
        data = b.get(kind) or {}
        text = plain_text(data.get("rich_text", []))
        number = number + 1 if kind == "numbered_list_item" else 0
        if kind in ("heading_1", "heading_2", "heading_3"):
            lines.append("#" * int(kind[-1]) + " " + text)
        elif kind == "bulleted_list_item":
            lines.append(f"- {text}")
        elif kind == "numbered_list_item":
            lines.append(f"{number}. {text}")
        elif kind == "to_do":
            lines.append(f"- [{'x' if data.get('checked') else ' '}] {text}")
        elif kind == "quote":
            lines.append(f"> {text}")
        elif kind == "code":
            lines.append(f"```\n{text}\n```")
        elif kind == "divider":
            lines.append("---")
        elif kind == "toggle":
            lines.append(f"- {text}")
        elif text:
            lines.append(text)
        if children and b.get("has_children") and depth < MAX_BLOCK_DEPTH:
            inner = blocks_to_markdown(children(b["id"]), children, depth + 1)
            lines += [f"  {line}" for line in inner.splitlines()]
    return "\n".join(lines)


def _strip_marks(line: str) -> str:
    """行頭の箇条書き・見出し・引用の印だけを外す。`0.85 に改善` の数字は残す。"""
    line = re.sub(r"^\s*#{1,6}\s+", "", line)
    line = re.sub(r"^\s*>\s?", "", line)
    return re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line)


def summarize(text: str, limit: int = RESULT_LIMIT) -> str:
    """Task の「結果」欄に入れる要約。見出しや空行を飛ばし、最初の数行を使う。"""
    lines = [_strip_marks(line).replace("**", "").replace("`", "").strip() for line in text.splitlines()]
    joined = " / ".join(line for line in lines if line)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


class NotionStore:
    def __init__(self, notion: Notion, state_path: Path):
        if not state_path.exists():
            raise NotionError(f"{state_path} がありません。kei-agent-notion-setup --apply を先に実行してください")
        self.notion = notion
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise NotionError(f"{state_path} を読めません: {e}") from None
        self._task_sources: dict[str, str | None] = {}

    def schema_problems(self) -> list[str]:
        """Notion の項目が Kei Agent の使う形からずれていないか見る（起動時の確認）。"""
        return schema_problems(self.notion, self.state)

    def _db(self, key: str) -> dict:
        return self.state["databases"][key]

    def _query(self, key: str, body: dict) -> list[dict]:
        path = f"/data_sources/{self._db(key)['data_source_id']}/query"
        return self.notion.paginate("POST", path, {**body, "page_size": 100})

    def _create_page(self, key: str, properties: dict, markdown: str = "") -> dict:
        blocks = markdown_to_blocks(markdown)
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self._db(key)["data_source_id"]},
            "properties": properties,
            "children": blocks[:BLOCKS_PER_REQUEST],
        })
        append_blocks(self.notion, page["id"], blocks[BLOCKS_PER_REQUEST:])
        return page

    def page_markdown(self, page_id: str) -> str:
        return blocks_to_markdown(self.notion.children(page_id), self.notion.children)

    # テーマ

    def active_themes(self) -> list[tuple[str, str]]:
        rows = self._query("themes", {"filter": {"property": "Status", "status": {"equals": THEME_ACTIVE}}})
        return [(plain_text(_prop(r["properties"], "Name")["title"]), r["id"]) for r in rows]

    def theme_page_id(self, name: str) -> str | None:
        rows = self._query("themes", {"filter": {"property": "Name", "title": {"equals": workspace_name(name)}}})
        return rows[0]["id"] if rows else None

    def theme_tasks(self, theme: str, page_id: str) -> str | None:
        """テーマのページの子の「Task」DB の data_source_id。無ければ None（そのテーマは飛ばす）。"""
        if page_id not in self._task_sources:
            ids = [b["id"] for b in self.notion.children(page_id)
                   if b["type"] == "child_database" and b["child_database"]["title"] == TASKS_TITLE]
            if len(ids) > 1:
                raise NotionError(f"テーマ「{theme}」のページに「{TASKS_TITLE}」の DB が {len(ids)} つあります")
            self._task_sources[page_id] = (
                self.notion.request("GET", f"/databases/{ids[0]}")["data_sources"][0]["id"] if ids else None)
            if not ids:
                log.warning("テーマ「%s」のページに「%s」の DB がありません", theme, TASKS_TITLE)
        return self._task_sources[page_id]

    def _has_heading(self, page_id: str, heading: str) -> bool:
        return any(b["type"] == "heading_2" and plain_text(b["heading_2"]["rich_text"]) == heading
                   for b in self.notion.children(page_id))

    def ensure_theme(self, name: str) -> bool:
        """テーマの行がなければ作り、ページに見出しと Task・先行研究の DB を置く。作ったら True。"""
        name = workspace_name(name)
        existing = self.theme_page_id(name)
        if existing:
            # 作りかけ・古いページも整える（何度やっても同じ）
            create_theme_databases(self.notion, existing)
            if not self._has_heading(existing, PREMISES_HEADING):
                self.notion.request("PATCH", f"/blocks/{existing}/children",
                                    {"children": theme_page_blocks(), "position": {"type": "start"}})
            return False
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self._db("themes")["data_source_id"]},
            "properties": {"Name": {"title": rich_text(name)}, "Status": {"status": {"name": THEME_ACTIVE}},
                           "Start": {"date": {"start": date.today().isoformat()}}},
            "children": theme_page_blocks(),
        })
        create_theme_databases(self.notion, page["id"])
        return True

    # Task

    def _task(self, page: dict, theme: str) -> Task:
        props = page["properties"]
        return Task(
            page["id"], plain_text(_prop(props, "Title")["title"]),
            (_prop(props, "Status").get("status") or {}).get("name", ""),
            (_prop(props, "Owner").get("select") or {}).get("name"),
            (_prop(props, "Due").get("date") or {}).get("start"),
            plain_text(_prop(props, "Work & Result").get("rich_text") or []),
            _prop(props, "Slack").get("url"), theme, page.get("url"))

    def _tasks(self, body: dict) -> list[Task]:
        """In progress のテーマを順に開き、それぞれの Task の DB を同じ条件で読む。"""
        found = []
        for theme, page_id in self.active_themes():
            source = self.theme_tasks(theme, page_id)
            if source is None:
                continue
            rows = self.notion.paginate("POST", f"/data_sources/{source}/query", {**body, "page_size": 100})
            found += [self._task(r, theme) for r in rows]
        return found

    def tonight_tasks(self, limit: int) -> list[Task]:
        return self._tasks({"filter": {"and": [
            {"property": "Owner", "select": {"equals": OWNER_KEI}},
            {"property": "Status", "status": {"equals": TONIGHT}},
        ]}, "sorts": [{"timestamp": "created_time", "direction": "ascending"}]})[:limit]

    def count_tonight_tasks(self) -> int:
        return len(self.tonight_tasks(10_000))

    def update_task(self, page_id: str, status: str | None = None, result: str | None = None,
                    slack_url: str | None = None) -> None:
        properties: dict = {}
        if status:
            properties["Status"] = {"status": {"name": status}}
        if result is not None:
            page = self.notion.request("GET", f"/pages/{page_id}")
            before = plain_text(_prop(page["properties"], "Work & Result").get("rich_text") or [])
            added = RESULT_PREFIX + result[:RESULT_LIMIT]
            properties["Work & Result"] = {"rich_text": rich_text(f"{before}\n{added}" if before else added)}
        if slack_url:
            properties["Slack"] = {"url": slack_url}
        self.notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def awaiting_tasks(self) -> list[Task]:
        return self._tasks({"filter": {"property": "Status", "status": {"equals": WAITING}}})

    def tasks_due_on(self, day: date) -> list[Task]:
        """その日が Due の Task。**済みも返す**（Daily では取り消し線にして、やったことも見せる）。"""
        return self._tasks({"filter": {"property": "Due", "date": {"equals": day.isoformat()}}})

    def tasks_due_within(self, today: date, days: int) -> list[Task]:
        tasks = self._tasks({"filter": {"and": [
            {"property": "Due", "date": {"on_or_before": (today + timedelta(days=days)).isoformat()}},
            {"property": "Status", "status": {"does_not_equal": DONE}},
        ]}})
        return sorted(tasks, key=lambda t: t.due or "")


def load_notion(config: Config, env: dict[str, str] | None = None) -> NotionStore | None:
    """ゲートウェイの合言葉と kei-agent-notion-setup の状態がそろっていれば NotionStore を返す。"""
    env = dict(os.environ) if env is None else env
    try:
        return NotionStore(gateway_notion("kei-agent", env, config), config.state_dir / "notion.json")
    except NotionError as e:
        log.warning("Notion にはつながない: %s", e)
        return None
