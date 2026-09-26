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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from kei_agent.config import Config
from kei_agent.notion import (
    BLOCKS_PER_REQUEST,
    Notion,
    NotionError,
    append_blocks,
    gateway_notion,
    schema_problems,
    theme_papers_view,
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


@dataclass
class Task:
    id: str
    title: str
    status: str
    assignee: str | None
    priority: str | None
    due: str | None
    slack_url: str | None
    theme_ids: list[str]
    url: str | None = None
    theme_names: list[str] = field(default_factory=list)


@dataclass
class Note:
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
            raise NotionError(f"{state_path} がありません。kei-agent-notion-setup を先に実行してください")
        self.notion = notion
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise NotionError(f"{state_path} を読めません: {e}") from None
        self._theme_names: dict[str, str] = {}

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

    def theme_page_id(self, name: str) -> str | None:
        rows = self._query("themes", {"filter": {"property": "名前", "title": {"equals": name}}})
        return rows[0]["id"] if rows else None

    def theme_name(self, page_id: str) -> str:
        if page_id not in self._theme_names:
            page = self.notion.request("GET", f"/pages/{page_id}")
            self._theme_names[page_id] = plain_text(_prop(page["properties"], "名前")["title"])
        return self._theme_names[page_id]

    def ensure_theme(self, name: str, slack_url: str, directory: str) -> bool:
        """テーマの行がなければ作る。作ったら True。そのページには、テーマの論文だけの表も置く。"""
        if self.theme_page_id(name):
            return False
        page = self._create_page("themes", {
            "名前": {"title": rich_text(name)},
            "状態": {"select": {"name": "進行中"}},
            "Slack": {"url": slack_url},
            "ディレクトリ": {"rich_text": rich_text(directory)},
        })
        if "papers" in self.state.get("databases", {}):
            try:
                self.notion.request("POST", "/views", theme_papers_view(self._db("papers"), page["id"]))
            except NotionError as e:
                log.warning("テーマのページに先行研究の表を置けません: %s", e)
        return True

    # 先行研究

    def paper_ids(self) -> list[str]:
        """先行研究 DB にある論文の ID（同じ論文を二度入れないため）。"""
        return [pid for row in self._query("papers", {})
                if (pid := plain_text(_prop(row["properties"], "ID").get("rich_text") or []))]

    def add_papers(self, theme: str, items: list[dict], source: str) -> int:
        """論文を先行研究 DB に「未読」で入れる。同じ ID の行があれば、テーマを足すだけ。書いた行の数を返す。"""
        theme_id = self.theme_page_id(theme)
        if theme_id is None:
            raise NotionError(f"研究ホームの「テーマ」に {theme} の行がありません")
        written = 0
        for item in items:
            paper_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if not paper_id or not title:
                continue
            rows = self._query("papers", {"filter": {"property": "ID", "rich_text": {"equals": paper_id}}})
            if rows:
                related = [r["id"] for r in _prop(rows[0]["properties"], "テーマ").get("relation") or []]
                if theme_id not in related:
                    self.notion.request("PATCH", f"/pages/{rows[0]['id']}", {"properties": {
                        "テーマ": {"relation": [{"id": i} for i in [*related, theme_id]]}}})
                    written += 1
                continue
            year = str(item.get("year") or "")
            self._create_page("papers", {
                "名前": {"title": rich_text(title[:200])},
                "URL": {"url": str(item.get("url") or "") or None},
                "ID": {"rich_text": rich_text(paper_id)},
                "著者": {"rich_text": rich_text(", ".join(str(a) for a in item.get("authors") or [])[:500])},
                "年": {"number": int(year) if year.isdigit() else None},
                "会場": {"rich_text": rich_text(str(item.get("venue") or ""))},
                "要点": {"rich_text": rich_text(str(item.get("summary") or ""))},
                "この研究との関係": {"rich_text": rich_text(str(item.get("relation") or ""))},
                "見つけた日": {"date": {"start": str(item.get("found") or date.today().isoformat())}},
                "出どころ": {"select": {"name": source}},
                "状態": {"select": {"name": "未読"}},
                "テーマ": {"relation": [{"id": theme_id}]},
            })
            written += 1
        return written

    # Task

    def _task(self, page: dict) -> Task:
        props = page["properties"]
        title = plain_text(_prop(props, "タイトル")["title"])
        status = (_prop(props, "状態").get("status") or {}).get("name", "")
        assignee = (_prop(props, "担当").get("select") or {}).get("name")
        priority = (_prop(props, "優先度").get("select") or {}).get("name")
        due = (_prop(props, "期日").get("date") or {}).get("start")
        theme_ids = [r["id"] for r in _prop(props, "テーマ").get("relation") or []]
        task = Task(page["id"], title, status, assignee, priority, due, _prop(props, "Slack").get("url"),
                    theme_ids, page.get("url"))
        task.theme_names = [self.theme_name(t) for t in theme_ids]
        return task

    def tonight_tasks(self, limit: int) -> list[Task]:
        rows = self._query("tasks", {
            "filter": {"and": [
                {"property": "担当", "select": {"equals": "Kei Agent"}},
                {"property": "状態", "status": {"equals": "今夜やる"}},
            ]},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        })
        return [self._task(r) for r in rows[:limit]]

    def count_tonight_tasks(self) -> int:
        return len(self._query("tasks", {"filter": {"and": [
            {"property": "担当", "select": {"equals": "Kei Agent"}},
            {"property": "状態", "status": {"equals": "今夜やる"}},
        ]}}))

    def task_by_slack_url(self, slack_url: str) -> Task | None:
        rows = self._query("tasks", {"filter": {"property": "Slack", "url": {"equals": slack_url}}})
        return self._task(rows[0]) if rows else None

    def create_night_task(self, title: str, theme_name: str, slack_url: str, body: str) -> Task:
        """Slack の 🌙 から Task を作る。同じメッセージの Task があれば「今夜やる」に戻す。"""
        existing = self.task_by_slack_url(slack_url)
        if existing:
            self.update_task(existing.id, status="今夜やる")
            existing.status = "今夜やる"
            return existing
        theme_id = self.theme_page_id(theme_name)
        properties = {
            "タイトル": {"title": rich_text(title)},
            "状態": {"status": {"name": "今夜やる"}},
            "担当": {"select": {"name": "Kei Agent"}},
            "Slack": {"url": slack_url},
        }
        if theme_id:
            properties["テーマ"] = {"relation": [{"id": theme_id}]}
        return self._task(self._create_page("tasks", properties, body))

    def cancel_night_task(self, slack_url: str) -> bool:
        """🌙 を外したら、まだ実行していない Task を「未着手」に戻す。"""
        task = self.task_by_slack_url(slack_url)
        if task is None or task.status != "今夜やる":
            return False
        self.update_task(task.id, status="未着手")
        return True

    def update_task(self, page_id: str, status: str | None = None, result: str | None = None,
                    slack_url: str | None = None) -> None:
        properties = {}
        if status:
            properties["状態"] = {"status": {"name": status}}
        if result is not None:
            properties["結果"] = {"rich_text": rich_text(result[:RESULT_LIMIT])}
        if slack_url:
            properties["Slack"] = {"url": slack_url}
        self.notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def awaiting_tasks(self) -> list[Task]:
        return [self._task(r) for r in self._query("tasks", {
            "filter": {"property": "状態", "status": {"equals": "確認待ち"}}})]

    def tasks_due_on(self, day: date) -> list[Task]:
        """その日が期日の Task。**済みも返す**（Daily では取り消し線にして、やったことも見せる）。"""
        rows = self._query("tasks", {
            "filter": {"property": "期日", "date": {"equals": day.isoformat()}},
            "sorts": [{"property": "期日", "direction": "ascending"}],
        })
        return [self._task(r) for r in rows]

    def tasks_due_within(self, today: date, days: int) -> list[Task]:
        rows = self._query("tasks", {
            "filter": {"and": [
                {"property": "期日", "date": {"on_or_before": (today + timedelta(days=days)).isoformat()}},
                {"property": "状態", "status": {"does_not_equal": "完了"}},
            ]},
            "sorts": [{"property": "期日", "direction": "ascending"}],
        })
        return [self._task(r) for r in rows]

    # ノートとマイルストーン

    def _note(self, page: dict, with_body: bool) -> Note:
        props = page["properties"]
        note = Note(
            page["id"], plain_text(_prop(props, "タイトル")["title"]),
            (_prop(props, "種類").get("select") or {}).get("name"),
            (_prop(props, "日付").get("date") or {}).get("start"),
            page.get("url"),
        )
        if with_body:
            note.body = self.page_markdown(page["id"])
        return note

    def notes_edited_since(self, since: datetime, kinds: list[str]) -> list[Note]:
        rows = self._query("notes", {
            "filter": {"and": [
                {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since.astimezone().isoformat()}},
                {"or": [{"property": "種類", "select": {"equals": k}} for k in kinds]},
            ]},
            "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
        })
        return [self._note(r, with_body=True) for r in rows]

    def upcoming_milestones(self, today: date, limit: int = 5) -> list[dict]:
        rows = self._query("milestones", {
            "filter": {"property": "期日", "date": {"on_or_after": today.isoformat()}},
            "sorts": [{"property": "期日", "direction": "ascending"}],
        })
        return [{"name": plain_text(_prop(r["properties"], "名前")["title"]),
                 "due": (_prop(r["properties"], "期日").get("date") or {}).get("start"),
                 "url": r.get("url")} for r in rows[:limit]]


def load_notion(config: Config, env: dict[str, str] | None = None) -> NotionStore | None:
    """ゲートウェイの合言葉と kei-agent-notion-setup の状態がそろっていれば NotionStore を返す。"""
    env = dict(os.environ) if env is None else env
    try:
        return NotionStore(gateway_notion("kei-agent", env, config), config.state_dir / "notion.json")
    except NotionError as e:
        log.warning("Notion にはつながない: %s", e)
        return None
