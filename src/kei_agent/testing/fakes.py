"""テストで使う偽物（通知先・AI・Notion・ジョブ）。本物には届かない。

どれも本体が使う相手と同じ呼び方を持つ（FakeSlack は通知先、FakeAI は runner.run_model、FakeNotion は
研究ホームの NotionStore、FakeHub は共通ホームの HubStore、FakeNotionAPI はゲートウェイの後ろの Notion、FakePueue は
ジョブの pueue）。モジュールのテストでは ModuleKit がまとめて用意する。
"""

from __future__ import annotations

import json

from kei_agent.execution import runner
from kei_agent.storage.notion import NotionError
from kei_agent.storage.notion_store import OWNER_KEI, TONIGHT, WAITING, Note, Task

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


class FakeSlack:
    """投稿と添付を記録する、テスト用の通知先。チャンネルの対応も持つ。"""

    def __init__(self, channels: dict[str, str]):
        self.channels = channels
        self.calls: list[tuple[str, dict]] = []
        self.replies: list[dict] = []
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

    def messages(self) -> list[dict]:
        """通知と答え（順に）。どれも channel・thread_ts・text を持つ。"""
        return [{"channel": kw.get("channel"), "thread_ts": kw.get("thread_ts"),
                 "text": kw.get("text") or kw.get("markdown_text") or ""} for kw in self.posted()]

    async def files_upload_v2(self, **kw):
        self.calls.append(("files_upload_v2", kw))

    async def conversations_list(self, **kw):
        channels = [{"id": cid, "name": name, "is_member": True} for cid, name in self.channels.items()]
        return {"channels": channels, "response_metadata": {"next_cursor": ""}}

class FakeAI:
    """AI の実行（runner.run_model）の代わり。Claude でも Codex でも同じ道を通る。

    `answer("…")` で次に返す答えを並べておける（並べていなければ「結果です」）。呼ばれた回は `calls` に残る
    （cwd・prompt・session_id・thread_ts・actor・use_case・provider・read_only）。細かい振る舞いは `behaviors` に dict で足す:
    text、steps（[("tool", "Bash: …")]）、side_effect（cwd を受け取る関数）、session_id、is_error、errors、raw
    （最後の答えの印で包まない）。
    """

    def __init__(self):
        self.calls = []
        self.behaviors = []

    def answer(self, text: str = "結果です", **behavior) -> FakeAI:
        """次に返す答え（何度も呼べば、その順に返す）。"""
        self.behaviors.append({"text": text, **behavior})
        return self

    def prompts(self) -> list[str]:
        return [call["prompt"] for call in self.calls]

    async def __call__(self, config, request, prompt, on_activity=None):
        ws = request.workspace
        self.calls.append({"cwd": ws.cwd, "prompt": prompt, "session_id": request.session_id,
                           "thread_ts": request.thread_ts, "actor": request.recipe.actor,
                           "use_case": str(request.recipe.use_case), "provider": request.recipe.provider,
                           "read_only": request.read_only})
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
        recipe = request.recipe
        result = runner.RunResult(provider=recipe.provider, actor=recipe.actor, use_case=str(recipe.use_case),
                                  model=recipe.model, effort=recipe.reasoning_effort)
        runner.apply_event(result, {
            "type": "result",
            "session_id": behavior.get("session_id", "sess-1"),
            "result": text,
            "is_error": behavior.get("is_error", False),
            "errors": behavior.get("errors", []),
        })
        return result


class FakeNotion:
    """NotionStore の代わり。テーマごとの Task をメモリに持つ。"""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.results: dict[str, str] = {}
        self.themes: set[str] = set()
        self.fail = False
        self._n = 0

    def _check(self):
        if self.fail:
            raise NotionError("503 Service Unavailable")

    def add_task(self, title, theme=None, status=TONIGHT, slack_url=None, body="", assignee=OWNER_KEI):
        self._n += 1
        task = Task(f"task-{self._n}", title, status, assignee, None, body, slack_url, theme,
                    f"https://notion.example/task-{self._n}")
        self.tasks[task.id] = task
        return task

    def tonight_tasks(self, limit):
        self._check()
        return [t for t in self.tasks.values() if t.owner == OWNER_KEI and t.status == TONIGHT][:limit]

    def count_tonight_tasks(self):
        self._check()
        return len(self.tonight_tasks(10_000))

    def update_task(self, page_id, status=None, result=None, slack_url=None):
        self._check()
        task = self.tasks[page_id]
        if status:
            task.status = status
        if result is not None:
            self.results[page_id] = result
        if slack_url:
            task.slack_url = slack_url

    def ensure_theme(self, name):
        self._check()
        if name in self.themes:
            return False
        self.themes.add(name)
        return True

    def awaiting_tasks(self):
        self._check()
        return [t for t in self.tasks.values() if t.status == WAITING]

    def tasks_due_on(self, day):
        """その日が期日の Task。済みも返す（Daily で取り消し線にするため）。"""
        return [t for t in self.tasks.values() if t.due == day.isoformat()]

    def tasks_due_within(self, today, days):
        self._check()
        return []


class FakeNotionAPI:
    """ゲートウェイの後ろに置く、手元だけの Notion（`kei_agent.storage.notion.Notion` と同じ forward / request を持つ）。

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
        from kei_agent.storage.notion_store import plain_text

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
        from kei_agent.storage.notion_store import plain_text

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
            from kei_agent.storage.notion_store import plain_text

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
        from kei_agent.storage.notion_store import plain_text

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


class FakeHub:
    has_time_db = True
    has_reading_db = True

    def __init__(self):
        self.readings: dict[str, dict] = {}
        self.trashed: list[str] = []
        self.notes = []
        self.appended = []
        self.reviews: dict[str, str] = {}
        self.edited: list[Note] = []
        self.minutes: dict[str, float] = {}
        self.known: set[str] = set()
        self.recorded = []
        self.calendar: list[dict] = []

    def upsert_day(self, kind, day, title, markdown, slack_url):
        note = Note(f"hub-{day}", title, kind, day, f"https://notion.example/hub/{day}", markdown)
        self.notes.append(note)
        return note

    def append_review_conclusion(self, page_id, text, stamp, message_id=None):
        self.appended.append((page_id, text))

    def reviews_edited_since(self, since):
        return list(self.edited)

    def review_text(self, day):
        return self.reviews.get(day, "")

    def time_minutes_by_domain(self, start, days=7):
        return dict(self.minutes)

    def time_url(self):
        return "https://www.notion.so/timedb"

    def time_ids_since(self, since):
        return set(self.known)

    def record_time(self, entry_id, domain, label, started_at, minutes, memo="", slack_url="", source="Slack"):
        self.recorded.append((entry_id, domain, label, minutes, source))

    def add_reading(self, item, day):
        page_id = f"reading-{len(self.readings) + 1}"
        self.readings[page_id] = {"item": item, "day": day}
        return page_id

    has_learning_db = True

    def add_learning(self, item, day, link=""):
        """学びのノートに入れる（learnings に ページ ID → 中身）。"""
        if not hasattr(self, "learnings"):
            self.learnings = {}
        page_id = f"learning-{len(self.learnings) + 1}"
        self.learnings[page_id] = {"item": item, "day": day, "link": link}
        return page_id, f"https://notion.example/{page_id}"

    def trash_page(self, page_id):
        self.trashed.append(page_id)

    collect = ([], [])

    def collect_settings(self):
        return self.collect

    def schema_problems(self):
        return []

    def calendar_rows(self, source, window_start, window_end):
        return [dict(row) for row in self.calendar if row["出典"] == source]

    def calendar_upsert(self, source, item, checked_at, existing_id=None):
        if existing_id:
            next(row for row in self.calendar if row["id"] == existing_id).update(
                {"名前": item.title, "日付": item.start, "同期状態": "確認済み"})
            return
        self.calendar.append({"id": f"cal-{len(self.calendar)}", "出典": source, "出典 ID": item.source_id,
                              "名前": item.title, "日付": item.start, "同期状態": "確認済み"})

    def calendar_mark_stale(self, row_id):
        next(row for row in self.calendar if row["id"] == row_id)["同期状態"] = "要確認"
