"""テストで使う偽物。"""

import json
from pathlib import Path

from kei_agent import runner
from kei_agent.jobs import REQUESTS_DIR
from kei_agent.notion import NotionError
from kei_agent.notion_store import Note, Task

# Notion API 2026-03-11 で消えたキー。送ったら落として気づけるようにする
_LEGACY_NOTION_KEYS = {"after": "position.after_block", "archived": "in_trash"}


def check_notion_body(body: dict | None) -> None:
    """旧 API のキー（after / archived）を送っていたら AssertionError にする。"""
    for key, new in _LEGACY_NOTION_KEYS.items():
        if body and key in body:
            raise AssertionError(f"Notion API 2026-03-11 では「{key}」ではなく「{new}」を使う: {body}")


class FakePueue:
    def __init__(self):
        self.added = []
        self.killed = []
        self.removed = []
        self.task_status: dict[int, dict] = {}

    async def add(self, cwd, command, label):
        task_id = len(self.added)
        self.added.append((cwd, command, label))
        self.task_status[task_id] = {"label": label,
                                     "status": {"Queued": {"enqueued_at": "2026-09-17T10:00:00+09:00"}}}
        return task_id

    async def kill(self, task_id):
        self.killed.append(task_id)

    async def remove(self, task_id):
        self.removed.append(task_id)
        self.task_status.pop(task_id, None)

    async def tasks(self):
        return dict(self.task_status)


def write_request(cwd: Path, **payload) -> Path:
    d = cwd / REQUESTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{payload['request_id']}.json"
    path.write_text(json.dumps(payload))
    return path


class FakeSlack:
    def __init__(self, channels: dict[str, str]):
        self.channels = channels
        self.calls: list[tuple[str, dict]] = []
        self.replies: list[dict] = []
        self.stream_modes: dict[str, str] = {}
        self._ts = 1000

    def _next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000"

    def posted(self) -> list[dict]:
        return [kw for name, kw in self.calls if name == "chat_postMessage"]

    def texts(self) -> list[str]:
        return [kw.get("text") or kw.get("markdown_text") for kw in self.posted()]

    async def conversations_info(self, channel):
        return {"channel": {"name": self.channels[channel]}}

    async def chat_postMessage(self, **kw):
        self.calls.append(("chat_postMessage", kw))
        return {"ts": self._next_ts()}

    async def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return {}

    # AI アプリ向けの表示

    async def agents_sessions_setStatus(self, **kw):
        self.calls.append(("agents_sessions_setStatus", kw))
        return {}

    async def assistant_threads_setStatus(self, **kw):
        self.calls.append(("assistant_threads_setStatus", kw))
        return {}

    async def chat_startStream(self, **kw):
        self.calls.append(("chat_startStream", kw))
        ts = self._next_ts()
        self.stream_modes[ts] = "chunks" if kw.get("chunks") else "markdown_text"
        return {"ts": ts}

    async def chat_appendStream(self, **kw):
        # 実物と同じく、始めたときと違う形（chunks と markdown_text）を混ぜると断る
        mode = "chunks" if kw.get("chunks") else "markdown_text"
        if self.stream_modes.get(kw["ts"], mode) != mode:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("streaming_mode_mismatch", {"ok": False, "error": "streaming_mode_mismatch"})
        self.calls.append(("chat_appendStream", kw))
        return {}

    async def chat_stopStream(self, **kw):
        self.calls.append(("chat_stopStream", kw))
        return {}

    def statuses(self) -> list[str]:
        return [kw["status"] for name, kw in self.calls if name == "agents_sessions_setStatus"]

    def thinking(self) -> list[str]:
        """「〇〇が入力中」の欄に出した文言。"""
        return [kw["status"] for name, kw in self.calls if name == "assistant_threads_setStatus"]

    def tasks(self) -> list[dict]:
        """流して見せた返事の中の、作業の手順（task_update）。"""
        return [c for name, kw in self.calls if name in ("chat_startStream", "chat_appendStream", "chat_stopStream")
                for c in kw.get("chunks") or [] if c["type"] == "task_update"]

    def streamed(self) -> list[str]:
        """流して見せた文章。start と append を順につないだもの。"""
        texts = []
        for name, kw in self.calls:
            if name in ("chat_startStream", "chat_appendStream", "chat_stopStream"):
                if kw.get("markdown_text"):
                    texts.append(kw["markdown_text"])
                texts += [c["text"] for c in kw.get("chunks") or [] if c["type"] == "markdown_text"]
        return texts

    async def reactions_add(self, **kw):
        self.calls.append(("reactions_add", kw))

    async def reactions_remove(self, **kw):
        self.calls.append(("reactions_remove", kw))

    async def views_publish(self, **kw):
        self.calls.append(("views_publish", kw))
        return {}

    async def views_open(self, **kw):
        self.calls.append(("views_open", kw))
        return {}

    async def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))

    async def conversations_replies(self, **kw):
        return {"messages": self.replies}

    async def conversations_list(self, **kw):
        channels = [{"id": cid, "name": name, "is_member": True} for cid, name in self.channels.items()]
        return {"channels": channels, "response_metadata": {"next_cursor": ""}}

    async def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://example.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}


class FakeClaude:
    """runner.run_model の代わり。解決済み execution request を記録して返す。"""

    def __init__(self):
        self.calls = []
        self.behaviors = []

    async def __call__(self, config, request, prompt, on_activity=None):
        ws = request.workspace
        self.calls.append({"cwd": ws.cwd, "prompt": prompt, "session_id": request.session_id,
                           "thread_ts": request.thread_ts})
        behavior = self.behaviors.pop(0) if self.behaviors else {}
        # 道具の呼び出し（途中の独り言は、実物と同じく本体へ流さない）
        for kind, value in behavior.get("steps", [("tool", "Bash: テスト")]):
            if kind == "tool" and on_activity:
                await on_activity(value)
        if "side_effect" in behavior:
            behavior["side_effect"](ws.cwd)
        # 実物と同じく、最後の result のイベントから組み立てる（上限の読み取りなども同じ道を通る）
        text = behavior.get("text", "結果です")
        if (text and not behavior.get("is_error", False) and not behavior.get("raw", False)
                and "<<kei-agent-final>>" not in text and "<<kei-agent-final-end>>" not in text):
            text = f"<<kei-agent-final>>\n{text}\n<<kei-agent-final-end>>"
        result = runner.RunResult(provider=request.recipe.provider)
        runner.apply_event(result, {
            "type": "result",
            "session_id": behavior.get("session_id", "sess-1"),
            "result": text,
            "is_error": behavior.get("is_error", False),
            "errors": behavior.get("errors", []),
        })
        return result


class FakeNotion:
    """NotionStore の代わり。Task とノートをメモリに持つ。"""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.bodies: dict[str, str] = {}
        self.results: dict[str, str] = {}
        self.notes: list[Note] = []
        self.themes: dict[str, str] = {}
        # 先行研究 DB（ID → 行）
        self.papers: dict[str, dict] = {}
        self.fail = False
        self._n = 0

    def _check(self):
        if self.fail:
            raise NotionError("503 Service Unavailable")

    def paper_ids(self):
        self._check()
        return list(self.papers)

    def add_papers(self, theme, items, source):
        self._check()
        for item in items:
            row = self.papers.setdefault(item["id"], {**item, "themes": [], "source": source, "state": "未読"})
            if theme not in row["themes"]:
                row["themes"].append(theme)
        return len(items)

    def add_task(self, title, theme=None, status="今夜やる", slack_url=None, body="", assignee="Kei Agent"):
        self._n += 1
        task = Task(f"task-{self._n}", title, status, assignee, "P1", None, slack_url,
                    [f"theme-{theme}"] if theme else [], f"https://notion.example/task-{self._n}",
                    [theme] if theme else [])
        self.tasks[task.id] = task
        self.bodies[task.id] = body
        return task

    def create_night_task(self, title, theme_name, slack_url, body):
        self._check()
        for t in self.tasks.values():
            if t.slack_url == slack_url:
                t.status = "今夜やる"
                return t
        return self.add_task(title, theme_name, slack_url=slack_url, body=body)

    def cancel_night_task(self, slack_url):
        self._check()
        for t in self.tasks.values():
            if t.slack_url == slack_url and t.status == "今夜やる":
                t.status = "未着手"
                return True
        return False

    def tonight_tasks(self, limit):
        self._check()
        return [t for t in self.tasks.values() if t.assignee == "Kei Agent" and t.status == "今夜やる"][:limit]

    def count_tonight_tasks(self):
        self._check()
        return len([t for t in self.tasks.values() if t.assignee == "Kei Agent" and t.status == "今夜やる"])

    def update_task(self, page_id, status=None, result=None, slack_url=None):
        self._check()
        task = self.tasks[page_id]
        if status:
            task.status = status
        if result is not None:
            self.results[page_id] = result
        if slack_url:
            task.slack_url = slack_url

    def page_markdown(self, page_id):
        return self.bodies.get(page_id, "")

    def ensure_theme(self, name, slack_url, directory):
        self._check()
        if name in self.themes:
            return False
        self.themes[name] = slack_url
        return True

    def notes_edited_since(self, since, kinds):
        self._check()
        return [n for n in self.notes if n.kind in kinds]

    def awaiting_tasks(self):
        self._check()
        return [t for t in self.tasks.values() if t.status == "確認待ち"]

    def tasks_due_on(self, day):
        """その日が期日の Task。済みも返す（Daily で取り消し線にするため）。"""
        return [t for t in self.tasks.values() if t.due == day.isoformat()]

    def tasks_due_within(self, today, days):
        self._check()
        return []

    def upcoming_milestones(self, today, limit=5):
        self._check()
        return [{"name": "中間発表", "due": "2026-10-01", "url": "https://notion.example/m1"}]


def _gh_flags(args: tuple[str, ...]) -> dict[str, str]:
    """gh の `--name value` の組を拾う。"""
    return {args[i][2:]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")}


class FakeGitHub:
    """issues.gh の代わり。本物の GitHub（公開リポジトリ）には届かない。conftest がすべてのテストで差し替える。

    `fail` に操作の先頭2語（`issue create` など）と例外を入れると、その操作だけ失敗させられる。
    """

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.fail: dict[str, Exception] = {}
        self._number = 0

    async def __call__(self, config, *args: str) -> str:
        self.calls.append(args)
        error = self.fail.get(" ".join(args[:2]))
        if error is not None:
            raise error
        if args[:2] == ("issue", "create"):
            self._number += 1
            return f"https://github.com/97kuek/kei-agent/issues/{self._number}\n"
        return ""

    def created(self) -> list[dict[str, str]]:
        """作った issue の title・body・label。"""
        return [_gh_flags(args) for args in self.calls if args[:2] == ("issue", "create")]

    def closed(self) -> list[tuple[str, str]]:
        """閉じた issue の番号と、添えたコメント。"""
        return [(args[2], _gh_flags(args).get("comment", "")) for args in self.calls if args[:2] == ("issue", "close")]


class FakeNotionAPI:
    """ゲートウェイの後ろに置く、手元だけの Notion（`kei_agent.notion.Notion` と同じ forward / request を持つ）。

    ページ・ブロック・データベース・データソース・ビューを親つきで持つ。ゲートウェイが中継した要求は
    `forwarded`、届く範囲を確かめるための読み取りは `lookups` に残す。本物の Notion には届かない。
    """

    _LISTS = frozenset({"title", "rich_text", "multi_select", "relation", "people", "files"})

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        self.rows: dict[str, list[str]] = {}
        self.markdown: dict[str, str] = {}
        self.forwarded: list[tuple[str, str, dict | None]] = []
        self.lookups: list[str] = []
        # 次の forward で返す失敗（(状態, 本文, ヘッダー) か例外）
        self.fail_next: list = []
        self._n = 0

    # 作る

    @staticmethod
    def key(item_id: str) -> str:
        return str(item_id).replace("-", "").lower()

    def new_id(self) -> str:
        import uuid

        self._n += 1
        return str(uuid.UUID(int=(0x4B31 << 112) | self._n))

    def _put(self, item: dict, parent_key: str | None = None) -> str:
        self.items[self.key(item["id"])] = item
        if parent_key is not None:
            self.children.setdefault(parent_key, []).append(self.key(item["id"]))
        return item["id"]

    def add_page(self, parent: str | None = None, title: str = "", *, data_source: str | None = None,
                 properties: dict | None = None, markdown: str = "") -> str:
        page_id = self.new_id()
        if data_source:
            ds = self.items[self.key(data_source)]
            parent_obj = {"type": "data_source_id", "data_source_id": ds["id"],
                          "database_id": ds["parent"]["database_id"]}
            # 本物と同じく、データソースの行は全部の列を（空の値でも）持つ
            empty = {name: {spec["type"]: [] if spec["type"] in self._LISTS else None}
                     for name, spec in ds["properties"].items()}
            props = self._props({**empty, **(properties or {})}, ds["properties"])
            self.rows.setdefault(self.key(data_source), []).append(self.key(page_id))
            parent_key = None
        else:
            parent_obj = ({"type": "page_id", "page_id": parent} if parent
                          else {"type": "workspace", "workspace": True})
            props = self._props(properties or {"title": {"title": [{"text": {"content": title}}]}},
                                {"title": {"id": "title", "type": "title"}})
            parent_key = self.key(parent) if parent else None
        self._put({"object": "page", "id": page_id, "parent": parent_obj, "properties": props, "in_trash": False,
                   "url": f"https://www.notion.so/{self.key(page_id)}"}, parent_key)
        self.markdown[self.key(page_id)] = markdown
        return page_id

    def add_block(self, parent: str, kind: str = "paragraph", text: str = "", **extra) -> str:
        block_id = self.new_id()
        parent_obj = ({"type": "page_id", "page_id": parent} if self.items[self.key(parent)]["object"] == "page"
                      else {"type": "block_id", "block_id": parent})
        self._put({"object": "block", "id": block_id, "parent": parent_obj, "type": kind, "has_children": False,
                   kind: {"rich_text": self._rich([{"text": {"content": text}}]), **extra}}, self.key(parent))
        return block_id

    def add_database(self, parent: str, title: str, properties: dict | None = None) -> tuple[str, str]:
        """(データベース, データソース) を作る。"""
        db_id, ds_id = self.new_id(), self.new_id()
        schema = self._schema(properties or {"名前": {"title": {}}})
        self._put({"object": "database", "id": db_id, "parent": {"type": "page_id", "page_id": parent},
                   "title": self._rich([{"text": {"content": title}}]), "url": f"https://www.notion.so/{self.key(db_id)}",
                   "data_sources": [{"id": ds_id, "name": title}]}, self.key(parent))
        self._put({"object": "data_source", "id": ds_id, "parent": {"type": "database_id", "database_id": db_id},
                   "title": self._rich([{"text": {"content": title}}]), "properties": schema})
        return db_id, ds_id

    def add_view(self, database: str, name: str, kind: str = "table") -> str:
        db = self.items[self.key(database)]
        view_id = self.new_id()
        self._put({"object": "view", "id": view_id, "parent": {"type": "database_id", "database_id": db["id"]},
                   "name": name, "type": kind, "data_source_id": db["data_sources"][0]["id"],
                   "url": f"https://www.notion.so/{self.key(database)}?v={self.key(view_id)}"})
        return view_id

    @staticmethod
    def _rich(items: list[dict]) -> list[dict]:
        return [{**item, "type": item.get("type", "text"),
                 "plain_text": item.get("plain_text") or (item.get("text") or {}).get("content", "")}
                for item in items]

    def _schema(self, properties: dict) -> dict:
        schema = {}
        for name, spec in properties.items():
            kind = next(iter(spec))
            schema[name] = {"id": self.key(self.new_id())[:4], "name": name, "type": kind, kind: spec[kind]}
        return schema

    def _props(self, values: dict, schema: dict) -> dict:
        props = {}
        for name, value in values.items():
            kind = value.get("type") or next(k for k in value if k not in ("id", "type"))
            data = value.get(kind)
            if kind in ("title", "rich_text"):
                data = self._rich(data or [])
            props[name] = {"id": (schema.get(name) or {}).get("id", name), "type": kind, kind: data}
        return props

    # 返す

    def _block_view(self, item: dict) -> dict:
        """ブロックの API から見た形（ページは child_page、データベースは child_database）。"""
        from kei_agent.notion_store import plain_text

        if item["object"] == "page":
            title = next((plain_text(p.get("title") or []) for p in item["properties"].values()
                          if p.get("type") == "title"), "")
            return {"object": "block", "id": item["id"], "parent": item["parent"], "type": "child_page",
                    "has_children": True, "in_trash": item["in_trash"], "child_page": {"title": title}}
        if item["object"] == "database":
            return {"object": "block", "id": item["id"], "parent": item["parent"], "type": "child_database",
                    "has_children": False, "child_database": {"title": plain_text(item["title"])}}
        return item

    @staticmethod
    def _error(status: int, code: str = "object_not_found", message: str = "not found"):
        return status, json.dumps({"object": "error", "status": status, "code": code, "message": message}).encode(), {}

    def _ok(self, payload: dict):
        return 200, json.dumps(payload).encode(), {}

    def _get(self, key: str, *objects: str) -> dict | None:
        item = self.items.get(self.key(key))
        return item if item is not None and item["object"] in objects else None

    # 受ける

    def forward(self, method: str, path: str, data: bytes | None):
        body = json.loads(data) if data else None
        self.forwarded.append((method, path, body))
        if self.fail_next:
            outcome = self.fail_next.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return self._handle(method, path, body)

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.lookups.append(path)
        status, payload, _ = self._handle(method, path, body)
        if status >= 400:
            raise NotionError(f"{method} {path}: {status}", status)
        return json.loads(payload)

    def _handle(self, method: str, path: str, body: dict | None):
        import urllib.parse

        path, _, raw_query = path.partition("?")
        query = dict(urllib.parse.parse_qsl(raw_query))
        parts = path.strip("/").split("/")
        head, item_id, tail = parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""
        body = body or {}
        if head == "search" and method == "POST":
            text = str(body.get("query") or "").lower()
            kinds = {(body.get("filter") or {}).get("value")} - {None} or {"page", "data_source"}
            found = [item for item in self.items.values()
                     if item["object"] in kinds and text in self._title(item).lower()]
            return self._ok({"object": "list", "results": found, "has_more": False, "next_cursor": None})
        if head == "pages":
            return self._pages(method, item_id, tail, body)
        if head == "blocks":
            return self._blocks(method, item_id, tail, body, query)
        if head == "databases":
            return self._databases(method, item_id, body)
        if head == "data_sources":
            return self._data_sources(method, item_id, tail, body)
        if head == "views":
            return self._views(method, item_id, body, query)
        return self._error(400, "invalid_request_url", "Invalid request URL.")

    def _title(self, item: dict) -> str:
        from kei_agent.notion_store import plain_text

        if isinstance(item.get("title"), list):
            return plain_text(item["title"])
        return next((plain_text(p.get("title") or []) for p in (item.get("properties") or {}).values()
                     if p.get("type") == "title"), "")

    def _pages(self, method, item_id, tail, body):
        if method == "POST" and not item_id:
            parent = body["parent"]
            if parent.get("type") == "data_source_id" or parent.get("data_source_id"):
                page_id = self.add_page(data_source=parent["data_source_id"], properties=body.get("properties"),
                                        markdown=body.get("markdown", ""))
            else:
                page_id = self.add_page(parent["page_id"], properties=body.get("properties"),
                                        markdown=body.get("markdown", ""))
            for child in body.get("children") or []:
                self._append(page_id, child)
            return self._ok(self.items[self.key(page_id)])
        page = self._get(item_id, "page")
        if page is None:
            return self._error(404)
        if tail == "move" and method == "POST":
            parent = body["parent"]
            old = next((key for key, kids in self.children.items() if self.key(item_id) in kids), None)
            if old:
                self.children[old].remove(self.key(item_id))
            if parent.get("type") == "data_source_id":
                ds = self._get(parent["data_source_id"], "data_source")
                page["parent"] = {"type": "data_source_id", "data_source_id": ds["id"],
                                  "database_id": ds["parent"]["database_id"]}
                self.rows.setdefault(self.key(ds["id"]), []).append(self.key(item_id))
            else:
                page["parent"] = {"type": "page_id", "page_id": parent["page_id"]}
                self.children.setdefault(self.key(parent["page_id"]), []).append(self.key(item_id))
            return self._ok(page)
        if tail == "markdown":
            if method == "PATCH":
                self.markdown[self.key(item_id)] = body["replace_content"]["new_str"]
            text = self.markdown.get(self.key(item_id), "")
            return self._ok({"object": "page_markdown", "id": page["id"], "markdown": text, "truncated": False,
                             "unknown_block_ids": []})
        if method == "PATCH":
            if "properties" in body:
                schema = page["properties"]
                page["properties"].update(self._props(body["properties"], schema))
            for key in ("in_trash", "icon"):
                if key in body:
                    page[key] = body[key]
        return self._ok(page)

    def _append(self, parent_id: str, child: dict, after: str | None = None) -> dict:
        kind = child["type"]
        block_id = self.new_id()
        parent = self.items[self.key(parent_id)]
        block = {"object": "block", "id": block_id, "type": kind, "has_children": False,
                 "parent": ({"type": "page_id", "page_id": parent["id"]} if parent["object"] == "page"
                            else {"type": "block_id", "block_id": parent["id"]}),
                 kind: {**child.get(kind, {}), "rich_text": self._rich(child.get(kind, {}).get("rich_text") or [])}}
        self.items[self.key(block_id)] = block
        kids = self.children.setdefault(self.key(parent_id), [])
        kids.insert(kids.index(self.key(after)) + 1 if after else len(kids), self.key(block_id))
        return block

    def _blocks(self, method, item_id, tail, body, query):
        item = self.items.get(self.key(item_id))
        if item is None or item["object"] not in ("page", "block", "database"):
            return self._error(404)
        if tail == "children":
            if method == "PATCH":
                after = ((body.get("position") or {}).get("after_block") or {}).get("id")
                added = []
                for child in body["children"]:
                    added.append(self._append(item_id, child, after))
                    after = added[-1]["id"]
                return self._ok({"object": "list", "results": added})
            kids = [self._block_view(self.items[key]) for key in self.children.get(self.key(item_id), [])
                    if not self.items[key].get("in_trash")]
            return self._ok({"object": "list", "results": kids, "has_more": False, "next_cursor": None})
        if method == "DELETE" or "in_trash" in body:
            item["in_trash"] = True if method == "DELETE" else body["in_trash"]
        elif method == "PATCH":
            item.setdefault(item.get("type"), {}).update(body.get(item.get("type"), {}))
        return self._ok(self._block_view(item))

    def _databases(self, method, item_id, body):
        if method == "POST":
            parent = body["parent"]["page_id"]
            from kei_agent.notion_store import plain_text

            db_id, _ = self.add_database(parent, plain_text(body.get("title") or []),
                                         (body.get("initial_data_source") or {}).get("properties"))
            return self._ok(self.items[self.key(db_id)])
        db = self._get(item_id, "database")
        if db is None:
            return self._error(404)
        if method == "PATCH":
            if "title" in body:
                db["title"] = self._rich(body["title"])
            if "parent" in body:
                db["parent"] = body["parent"]
        return self._ok(db)

    def _data_sources(self, method, item_id, tail, body):
        ds = self._get(item_id, "data_source")
        if ds is None:
            return self._error(404)
        if tail == "query":
            rows = [self.items[key] for key in self.rows.get(self.key(item_id), [])
                    if not self.items[key].get("in_trash") and self._matches(self.items[key]["properties"],
                                                                             body.get("filter"))]
            return self._ok({"object": "list", "results": rows, "has_more": False, "next_cursor": None})
        if tail == "templates":
            return self._ok({"templates": [], "has_more": False})
        if method == "PATCH" and "properties" in body:
            for name, spec in body["properties"].items():
                if spec is None:
                    ds["properties"].pop(name, None)
                elif set(spec) == {"name"}:
                    old = next(key for key, prop in ds["properties"].items() if prop["id"] == name or key == name)
                    ds["properties"][spec["name"]] = {**ds["properties"].pop(old), "name": spec["name"]}
                else:
                    ds["properties"].update(self._schema({name: spec}))
        return self._ok(ds)

    def _views(self, method, item_id, body, query):
        if not item_id and method == "GET":
            database = query.get("database_id")
            source = query.get("data_source_id")
            found = [{"object": "view", "id": item["id"]} for item in self.items.values()
                     if item["object"] == "view"
                     and (not database or self.key(item["parent"]["database_id"]) == self.key(database))
                     and (not source or self.key(item["data_source_id"]) == self.key(source))]
            return self._ok({"object": "list", "results": found, "has_more": False, "next_cursor": None})
        if not item_id and method == "POST":
            # 本物の API と同じく、置き場所の指定はどれか1つだけ
            if sum(key in body for key in ("database_id", "view_id", "create_database")) != 1:
                return self._error(400, "validation_error",
                                   "Exactly one of database_id, view_id, or create_database must be provided.")
            database = body.get("database_id")
            if "create_database" in body:
                page = body["create_database"]["parent"]["page_id"]
                database = self.new_id()
                self._put({"object": "database", "id": database, "parent": {"type": "page_id", "page_id": page},
                           "title": self._rich([{"text": {"content": body.get("name", "")}}]),
                           "data_sources": [{"id": body["data_source_id"], "name": body.get("name", "")}]},
                          self.key(page))
            view_id = self.add_view(database, body.get("name", ""), body.get("type", "table"))
            self.items[self.key(view_id)]["data_source_id"] = body.get("data_source_id")
            return self._ok(self.items[self.key(view_id)])
        view = self._get(item_id, "view")
        if view is None:
            return self._error(404)
        if method == "DELETE":
            self.items.pop(self.key(item_id))
        elif method == "PATCH":
            view.update(body)
        return self._ok(view)

    def _matches(self, props: dict, flt: dict | None) -> bool:
        """データソースの絞り込み（equals と日付の比較だけ。ほかの条件は通す）。"""
        from kei_agent.notion_store import plain_text

        if not flt:
            return True
        if "and" in flt:
            return all(self._matches(props, item) for item in flt["and"])
        if "or" in flt:
            return any(self._matches(props, item) for item in flt["or"])
        prop = props.get(flt.get("property")) or {}
        for kind, condition in flt.items():
            if kind == "property" or not isinstance(condition, dict):
                continue
            data = prop.get(prop.get("type"))
            value = (plain_text(data or []) if kind in ("title", "rich_text")
                     else (data or {}).get("name") if kind in ("select", "status")
                     else (data or {}).get("start") if kind == "date" else data)
            if "equals" in condition:
                return value == condition["equals"]
            if "does_not_equal" in condition:
                return value != condition["does_not_equal"]
            if kind == "date" and value:
                day = str(value)[:10]
                checks = {"on_or_after": day.__ge__, "on_or_before": day.__le__, "after": day.__gt__,
                          "before": day.__lt__}
                return all(checks[op](str(bound)[:10]) for op, bound in condition.items() if op in checks)
        return True
