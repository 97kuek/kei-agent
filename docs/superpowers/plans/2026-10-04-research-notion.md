# 研究の Notion の作り直し Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 研究ホームを「テーマの DB ＋ テーマごとの Task と先行研究の DB」の形にし、Kei Agent のコード・Dot のプロンプト・Notion の中身を切り替える。

**Architecture:** 研究ホームの作りは `src/kei_agent/storage/notion.py`（定義と `kei-agent-notion-setup`）、読み書きは `src/kei_agent/storage/notion_store.py`（`NotionStore`）が持つ。テーマごとの DB は ID を `notion.json` に控えず、テーマのページの子の DB を決まった名前（`Task`・`先行研究`）で探す。値（Status など）は英語の定数を `notion_store.py` に置き、モジュールは `kei_agent.api` から使う。

**Tech Stack:** Python 3（uv）、pytest、ruff、Notion API（ゲートウェイ経由）、Dot（ChatGPT）のプロンプト

**Spec:** `docs/superpowers/specs/2026-10-04-research-notion-design.md`

## Global Constraints

- テーマの列: Name（title）・Status（status: In progress / On hold / Done）・Start（date）・End（date）・Duration（formula、日数。End が空なら今日まで）
- テーマのページの見出し: 「前提とキーワード」「進捗ログ」。子の DB の名前: 「Task」「先行研究」
- Task の列: Title（title）・Status（status: Not started / Tonight / Running / Waiting / Done）・Owner（select: Me / Kei）・Due（date）・Work & Result（rich_text）・Slack（url）
- 先行研究の列: Title（title）・Summary（rich_text）・Link（url、照合キー）・Status（select: Unread / Read / Use）
- なくす DB: ノート・マイルストーン・研究全体の Task と先行研究
- モジュールがコアに触れるのは `kei_agent.api` だけ（AGENTS.md）
- コメント・文書・Slack の文は日本語。文書には今の状態だけを書く
- 確かめる: `uv run python -m pytest` と `uvx ruff check .`（sandbox の外で）
- コミットの1行目は `feat:`・`fix:`・`docs:` などを頭に付けた英語の短い文
- 本物の Notion・Dot の設定に触れるのは Task 6・7 だけで、そのときも利用者の確認を取ってから

## Review Focus

- テーマのページに「Task」の DB が無い・2つある → 前者はそのテーマを飛ばして続け、後者は NotionError で止める（どちらのテーマか分かる文で）
- テーマの Status が On hold・Done → 夜間の Task も Daily の集計も、そのテーマを読まない
- Work & Result が空の Task を夜間に実行 → 「（作業の中身なし）」を頼みごとにして止まらない
- Work & Result に結果を書き足すと 2000 字を超える → 古い結果を切らずに、最後の結果を `RESULT_LIMIT` で丸めて足す
- テーマの名前がチャンネル名（`1-amr-query`）で渡される → 作業場の名前（`amr-query`）で照合する

---

## File Structure

| ファイル | 役目 | 変えること |
|---|---|---|
| `src/kei_agent/storage/notion.py` | 研究ホームの定義と setup | THEMES・THEME_TASKS・THEME_PAPERS にする。ノート・マイルストーン・研究全体の Task と先行研究・テンプレートの処理を消す。テーマのページの中身を作る関数を足す |
| `src/kei_agent/storage/notion_store.py` | 研究ホームの読み書き | 値の定数、テーマごとの DB を探す処理、Task の形を変える。ノート・マイルストーン・論文の処理を消す |
| `src/kei_agent/api.py` | モジュールの窓口 | 値の定数を出す |
| `src/kei_agent/testing/fakes.py` | 偽物 | `FakeNotion` を新しい Task の形と値にする |
| `modules/night/module.py` | 夜間の Task（Mac 側。今は止めてあるが残す） | 新しい Task の形と値にする |
| `src/kei_agent/scheduling/digest.py`・`modules/daily/texts.py` | Daily の材料 | ノートとマイルストーンを外し、値を英語にする |
| `src/kei_agent/operations/doctor.py`・`src/kei_agent/api.py` の説明 | 表示の文 | 「Task・ノート・先行研究」を新しい形の説明にする |
| `modules/research/plugin/skills/*/SKILL.md` | 研究担当の skill | 先行研究と Task の書き方を新しい列にする |
| `docs/` と `docs/prompts/` | 文書と Dot のプロンプト | 新しい形にする |
| `tests/test_notion_setup.py`（新規）・`tests/test_notion_store.py`・`tests/test_schedule.py`・`tests/test_daily_module.py` | テスト | 新しい形にする |

**順番の前提:** Task 6（Dot のプロンプト）は、PR [97kuek/kei-agent#24](https://github.com/97kuek/kei-agent/pull/24) を main に入れてから、このブランチに main を取り込んで進める。

---

### Task 1: 研究ホームの定義と setup

**Files:**
- Modify: `src/kei_agent/storage/notion.py:223-733`
- Create: `tests/test_notion_setup.py`

**Interfaces:**
- Produces:
  - `THEMES: dict`、`THEME_TASKS: dict`、`THEME_PAPERS: dict`（`properties` と `icon`・`description` を持つ。`relations` は持たない）
  - `TASKS_TITLE = "Task"`、`PAPERS_TITLE = "先行研究"`、`PREMISES_HEADING = "前提とキーワード"`、`LOG_HEADING = "進捗ログ"`
  - `SPECS = {"themes": THEMES}`（`notion.json` に控えるのはテーマの DB だけ）
  - `theme_page_blocks() -> list[dict]`（見出し2つと、それぞれの下の空の段落）
  - `create_theme_databases(notion, page_id: str) -> dict[str, str]`（`{"tasks": data_source_id, "papers": data_source_id}`。同名の DB があれば作らずにそれを返す）
  - `schema_problems(notion, state) -> list[str]`（テーマの DB に加え、各テーマのページの Task と先行研究の列も見る）

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_notion_setup.py
import json

import pytest

from kei_agent.storage.notion import (
    LOG_HEADING,
    PAPERS_TITLE,
    PREMISES_HEADING,
    TASKS_TITLE,
    THEME_PAPERS,
    THEME_TASKS,
    THEMES,
    Notion,
    NotionError,
    Setup,
    create_theme_databases,
    schema_problems,
    theme_page_blocks,
)
from kei_agent.testing.fakes import FakeNotionAPI


def client(api: FakeNotionAPI):
    """FakeNotionAPI に、Notion と同じ paginate と children を付ける。"""
    class Client:
        request = staticmethod(api.request)

        def paginate(self, method, path, body=None):
            return Notion.paginate(self, method, path, body)

        def children(self, block_id):
            return Notion.children(self, block_id)

    return Client()


def test_columns_follow_the_spec():
    assert list(THEMES["properties"]) == ["Name", "Status", "Start", "End", "Duration"]
    assert [o["name"] for o in THEMES["properties"]["Status"]["status"]["options"]] == ["In progress", "On hold", "Done"]
    assert "dateBetween" in THEMES["properties"]["Duration"]["formula"]["expression"]
    assert list(THEME_TASKS["properties"]) == ["Title", "Status", "Owner", "Due", "Work & Result", "Slack"]
    assert [o["name"] for o in THEME_TASKS["properties"]["Status"]["status"]["options"]] == [
        "Not started", "Tonight", "Running", "Waiting", "Done"]
    assert [o["name"] for o in THEME_TASKS["properties"]["Owner"]["select"]["options"]] == ["Me", "Kei"]
    assert list(THEME_PAPERS["properties"]) == ["Title", "Summary", "Link", "Status"]
    assert [o["name"] for o in THEME_PAPERS["properties"]["Status"]["select"]["options"]] == ["Unread", "Read", "Use"]


def test_theme_page_blocks_put_premises_before_the_log():
    headings = [b["heading_2"]["rich_text"][0]["text"]["content"] for b in theme_page_blocks() if b["type"] == "heading_2"]
    assert headings == [PREMISES_HEADING, LOG_HEADING]


def test_create_theme_databases_is_idempotent():
    api = FakeNotionAPI()
    page = api.add_page(title="amr-query")
    first = create_theme_databases(client(api), page)
    second = create_theme_databases(client(api), page)
    assert first == second and set(first) == {"tasks", "papers"}
    titles = [b["child_database"]["title"] for b in client(api).children(page) if b["type"] == "child_database"]
    assert titles == [TASKS_TITLE, PAPERS_TITLE]


def test_setup_makes_only_the_theme_database_and_the_strategy_page(tmp_path):
    api = FakeNotionAPI()
    home = api.add_page(title="研究ホーム")
    setup = Setup(client(api), home, tmp_path / "notion.json")
    setup.run()
    state = json.loads((tmp_path / "notion.json").read_text())
    assert set(state["databases"]) == {"themes"}
    kinds = [(b["type"], b.get("child_database", b.get("child_page", {})).get("title"))
             for b in client(api).children(home) if b["type"] in ("child_database", "child_page")]
    assert ("child_database", "テーマ") in kinds and ("child_page", "中長期の方針") in kinds
    assert not any(title in ("Task", "ノート", "先行研究") for _, title in kinds)


def test_schema_problems_reports_a_theme_without_its_task_database(tmp_path):
    api = FakeNotionAPI()
    home = api.add_page(title="研究ホーム")
    setup = Setup(client(api), home, tmp_path / "notion.json")
    setup.run()
    ds = setup.state["databases"]["themes"]["data_source_id"]
    api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": "amr-query"}}]},
                                             "Status": {"status": {"name": "In progress"}}})
    problems = schema_problems(client(api), setup.state)
    assert any("amr-query" in p and TASKS_TITLE in p for p in problems)


def test_two_task_databases_on_one_theme_page_stop_with_the_theme_named():
    api = FakeNotionAPI()
    page = api.add_page(title="amr-query")
    api.add_database(page, TASKS_TITLE, THEME_TASKS["properties"])
    api.add_database(page, TASKS_TITLE, THEME_TASKS["properties"])
    with pytest.raises(NotionError, match=TASKS_TITLE):
        create_theme_databases(client(api), page)
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_notion_setup.py -v`
Expected: FAIL（`ImportError: cannot import name 'THEME_TASKS'`）

- [ ] **Step 3: 定義を書き換える**

`src/kei_agent/storage/notion.py` の `THEMES`〜`theme_papers_view`（223-329 行目付近）を次に置き換え、`NOTE_TEMPLATES`・`_BLANK_TEMPLATE_NAMES`・`STRATEGY_TITLE` のマイルストーンの説明を消す（`STRATEGY_TITLE = "中長期の方針"` は残す）。

```python
def _status(*names_groups: tuple[str, str, str]) -> dict:
    return {"status": {"options": [{"name": n, "color": c, "group": g} for n, c, g in names_groups]}}


THEMES = {
    "icon": "🗂",
    "description": "1テーマ = Slack の1チャンネル = ~/research/<名前>/。前提とキーワード・進捗ログ・Task・先行研究は、テーマのページの中に置く。",
    "properties": {
        "Name": {"title": {}},
        "Status": _status(("In progress", "green", "In progress"), ("On hold", "yellow", "To-do"),
                          ("Done", "gray", "Complete")),
        "Start": {"date": {}},
        "End": {"date": {}},
        "Duration": {"formula": {"expression": (
            'if(empty(prop("Start")), "", if(empty(prop("End")), dateBetween(now(), prop("Start"), "days"), '
            'dateBetween(prop("End"), prop("Start"), "days")))')}},
    },
}

THEME_TASKS = {
    "icon": "✅",
    "description": "Owner が Kei で Status が Tonight の Task は、夜間の Task が実行する。Work & Result は「作業: …」「結果: …」で書く。",
    "properties": {
        "Title": {"title": {}},
        "Status": _status(("Not started", "gray", "To-do"), ("Tonight", "purple", "To-do"),
                          ("Running", "blue", "In progress"), ("Waiting", "orange", "In progress"),
                          ("Done", "green", "Complete")),
        "Owner": {"select": {"options": _options(("Me", "blue"), ("Kei", "green"))}},
        "Due": {"date": {}},
        "Work & Result": {"rich_text": {}},
        "Slack": {"url": {}},
    },
}

THEME_PAPERS = {
    "icon": "📚",
    "description": "このテーマの先行研究。Link で照合し、同じ論文を2行にしない（arXiv は abs の URL、バージョン番号は外す）。",
    "properties": {
        "Title": {"title": {}},
        "Summary": {"rich_text": {}},
        "Link": {"url": {}},
        "Status": {"select": {"options": _options(("Unread", "gray"), ("Read", "blue"), ("Use", "green"))}},
    },
}

TASKS_TITLE = "Task"
PAPERS_TITLE = "先行研究"
PREMISES_HEADING = "前提とキーワード"
LOG_HEADING = "進捗ログ"
STRATEGY_TITLE = "中長期の方針"
SPECS = {"themes": THEMES}
THEME_DATABASES = (("tasks", TASKS_TITLE, THEME_TASKS), ("papers", PAPERS_TITLE, THEME_PAPERS))


def theme_page_blocks() -> list[dict]:
    """テーマのページの先頭に置く見出しと、その下の空の段落。"""
    blocks = []
    for heading in (PREMISES_HEADING, LOG_HEADING):
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": heading}}]}})
        blocks.append({"type": "paragraph", "paragraph": {"rich_text": []}})
    return blocks


def _child_databases(notion, page_id: str, title: str) -> list[str]:
    return [b["id"] for b in notion.children(page_id)
            if b["type"] == "child_database" and b["child_database"]["title"] == title]


def create_theme_databases(notion, page_id: str) -> dict[str, str]:
    """テーマのページに Task と先行研究の DB を置く。あればそれを使う。{"tasks": ds, "papers": ds} を返す。"""
    found = {}
    for key, title, spec in THEME_DATABASES:
        ids = _child_databases(notion, page_id, title)
        if len(ids) > 1:
            raise NotionError(f"テーマのページ {page_id} に「{title}」の DB が {len(ids)} つあります。1つに整理してください")
        if ids:
            db_id = ids[0]
        else:
            db_id = notion.request("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": page_id},
                "title": [{"text": {"content": title}}],
                "description": [{"text": {"content": spec["description"]}}],
                "icon": {"type": "emoji", "emoji": spec["icon"]},
                "initial_data_source": {"properties": spec["properties"]},
            })["id"]
        found[key] = notion.request("GET", f"/databases/{db_id}")["data_sources"][0]["id"]
    return found
```

- [ ] **Step 4: `schema_problems` を書き換える**

```python
def _spec_problems(where: str, spec: dict, live: dict) -> list[str]:
    problems = []
    for name, want in spec["properties"].items():
        kind = next(iter(want))
        have = live.get(name)
        if have is None:
            problems.append(f"{where}: 項目「{name}」がありません")
        elif have.get("type") != kind:
            problems.append(f"{where}: 項目「{name}」の種類が {have.get('type')} になっています（{kind} のはず）")
        elif kind in ("select", "status"):
            missing = _option_names(want[kind]) - _option_names(have.get(kind, {}))
            if missing:
                problems.append(f"{where}: 項目「{name}」に選択肢 {'、'.join(sorted(missing))} がありません")
    return problems


def schema_problems(notion: Notion, state: dict) -> list[str]:
    """テーマの DB と、各テーマのページの Task・先行研究の列が、Kei Agent の使う形からずれていないか見る。"""
    db = (state.get("databases") or {}).get("themes")
    if db is None:
        return ["themes: notion.json にありません（kei-agent-notion-setup --apply を実行してください）"]
    try:
        live = notion.request("GET", f"/data_sources/{db['data_source_id']}")["properties"]
    except NotionError as e:
        return [f"themes: 読めません（{e}）"]
    problems = _spec_problems("themes", THEMES, live)
    if problems:
        return problems
    for row in notion.paginate("POST", f"/data_sources/{db['data_source_id']}/query", {"page_size": 100}):
        name = "".join(t.get("plain_text", "") for t in row["properties"]["Name"]["title"])
        for _, title, spec in THEME_DATABASES:
            ids = _child_databases(notion, row["id"], title)
            if len(ids) != 1:
                problems.append(f"テーマ「{name}」: 「{title}」の DB が {len(ids)} つあります（1つのはず）")
                continue
            ds = notion.request("GET", f"/databases/{ids[0]}")["data_sources"][0]["id"]
            problems += _spec_problems(f"テーマ「{name}」の{title}", spec,
                                       notion.request("GET", f"/data_sources/{ds}")["properties"])
    return problems
```

- [ ] **Step 5: `Setup` を簡単にする**

`Setup` から `theme_paper_views`・`pages_without_paper_table`・`templates`・`note_templates` を消し、`databases`・`view_specs`・`section_specs`・`run`・`plan` を次にする。

```python
    def databases(self) -> list[tuple[str, str, str, dict]]:
        return [("themes", self.home, "テーマ", THEMES)]

    @staticmethod
    def view_specs(dbs: dict[str, dict]) -> dict[str, list[dict]]:
        return {"themes": [{"name": "In progress", "type": "list", "filter": _eq_status("Status", "In progress")}]}

    @staticmethod
    def section_specs() -> list[tuple[str, str, dict]]:
        return [("進行中のテーマ", "themes", {
            "name": "進行中のテーマ", "type": "list", "filter": _eq_status("Status", "In progress")})]

    def run(self) -> None:
        self.notion.request("PATCH", f"/pages/{self.home}", {"icon": {"type": "emoji", "emoji": "🔬"}})
        self.state["strategy_page_id"] = self.child_page(self.home, STRATEGY_TITLE, "🧭")
        dbs = {key: self.database(key, parent, title, spec) for key, parent, title, spec in self.databases()}
        for key, specs in self.view_specs(dbs).items():
            self.views(dbs[key], specs)
        for row in self.notion.paginate("POST", f"/data_sources/{dbs['themes']['data_source_id']}/query",
                                        {"page_size": 100}):
            create_theme_databases(self.notion, row["id"])
        self.home_sections([(title, dbs[key], view) for title, key, view in self.section_specs()])
        self.save()

    def plan(self) -> list[str]:
        lines: list[str] = []
        if not any(b["type"] == "child_page" and b["child_page"]["title"] == STRATEGY_TITLE
                   for b in self.notion.children(self.home)):
            lines.append(f"ページを作る: {STRATEGY_TITLE}")
        for key, parent, title, spec in self.databases():
            db_id = self.find_database(parent, title)
            if db_id is None:
                lines.append(f"データベースを作る: {title}（ビューも作る）")
                continue
            ds_id = self.notion.request("GET", f"/databases/{db_id}")["data_sources"][0]["id"]
            ds = self.notion.request("GET", f"/data_sources/{ds_id}")
            if missing := [n for n in spec["properties"] if n not in ds["properties"]]:
                lines.append(f"列を足す: {title}（{'、'.join(missing)}）")
            for row in self.notion.paginate("POST", f"/data_sources/{ds_id}/query", {"page_size": 100}):
                absent = [t for _, t, _ in THEME_DATABASES if not _child_databases(self.notion, row["id"], t)]
                if absent:
                    lines.append(f"テーマのページに DB を置く: {row['id']}（{'、'.join(absent)}）")
        if missing := [t for t, _, _ in self.section_specs() if t not in self.home_headings()]:
            lines.append(f"ホームに見出しとビューを足す: {'、'.join(missing)}")
        return lines
```

`database()` の中の `spec.get("relations", {})` の2か所は、`relations` を持つ定義が無くなるので消す。

- [ ] **Step 6: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_notion_setup.py -v`
Expected: PASS（6件）

- [ ] **Step 7: コミット**

```bash
git add src/kei_agent/storage/notion.py tests/test_notion_setup.py
git commit -m "feat: define the theme-scoped research Notion layout"
```

---

### Task 2: テーマごとの読み書き（NotionStore）

**Files:**
- Modify: `src/kei_agent/storage/notion_store.py:17-443`
- Modify: `src/kei_agent/api.py:47,58-59`
- Modify: `tests/test_notion_store.py:51-122,162-177`

**Interfaces:**
- Consumes: Task 1 の `THEMES`・`THEME_TASKS`・`TASKS_TITLE`・`PAPERS_TITLE`・`theme_page_blocks`・`create_theme_databases`・`schema_problems`
- Produces:
  - 定数 `NOT_STARTED = "Not started"`・`TONIGHT = "Tonight"`・`RUNNING = "Running"`・`WAITING = "Waiting"`・`DONE = "Done"`・`OWNER_ME = "Me"`・`OWNER_KEI = "Kei"`・`THEME_ACTIVE = "In progress"`・`THEME_ON_HOLD = "On hold"`（`kei_agent.api` からも出す）
  - `@dataclass Task(id: str, title: str, status: str, owner: str | None, due: str | None, work: str, slack_url: str | None, theme: str | None, url: str | None = None)`
  - `NotionStore.active_themes() -> list[tuple[str, str]]`（(名前, ページ ID)、Status が In progress のものだけ）
  - `NotionStore.theme_tasks(theme: str, page_id: str) -> str | None`（Task の DB の data_source_id。無ければ None）
  - `NotionStore.ensure_theme(name: str) -> bool`
  - `NotionStore.tonight_tasks(limit: int) -> list[Task]`・`count_tonight_tasks() -> int`・`awaiting_tasks() -> list[Task]`・`tasks_due_on(day: date) -> list[Task]`・`tasks_due_within(today: date, days: int) -> list[Task]`
  - `NotionStore.update_task(page_id: str, status: str | None = None, result: str | None = None, slack_url: str | None = None) -> None`（`result` は Work & Result に「結果: …」として書き足す）
  - 消すもの: `paper_ids`・`add_papers`・`create_night_task`・`task_by_slack_url`・`notes_edited_since`・`upcoming_milestones`・`Note`・`theme_name`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_notion_store.py` の `RecordingNotion`・`state` フィクスチャ・`test_create_night_task_links_theme`・`test_missing_property_is_reported_as_a_notion_error`・`test_schema_problems_reports_renamed_options_and_missing_columns_and_databases` を消し、次を足す。

```python
from datetime import date

from kei_agent.storage.notion import THEMES, create_theme_databases
from kei_agent.storage.notion_store import DONE, OWNER_KEI, OWNER_ME, TONIGHT, WAITING, NotionStore
from kei_agent.testing.fakes import FakeNotionAPI


def _client(api):
    class Client:
        request = staticmethod(api.request)

        def paginate(self, method, path, body=None):
            return Notion.paginate(self, method, path, body)

        def children(self, block_id):
            return Notion.children(self, block_id)

    return Client()


@pytest.fixture
def home(tmp_path):
    """テーマの DB が1つある研究ホームと、それを読む NotionStore。"""
    api = FakeNotionAPI()
    page = api.add_page(title="研究ホーム")
    db, ds = api.add_database(page, "テーマ", THEMES["properties"])
    path = tmp_path / "notion.json"
    path.write_text(json.dumps({"databases": {"themes": {"database_id": db, "data_source_id": ds, "properties": {}}}}))
    return api, ds, NotionStore(_client(api), path)


def _theme(api, ds, name, status="In progress"):
    page = api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": name}}]},
                                                    "Status": {"status": {"name": status}}})
    return page, create_theme_databases(_client(api), page)


def _task(api, tasks_ds, title, status=TONIGHT, owner=OWNER_KEI, due=None, work="作業: 条件Cを回す"):
    props = {"Title": {"title": [{"text": {"content": title}}]}, "Status": {"status": {"name": status}},
             "Owner": {"select": {"name": owner}}, "Work & Result": {"rich_text": [{"text": {"content": work}}]}}
    if due:
        props["Due"] = {"date": {"start": due}}
    return api.add_page(data_source=tasks_ds, properties=props)


def test_tonight_tasks_come_only_from_active_themes(home):
    api, ds, store = home
    _, active = _theme(api, ds, "amr-query")
    _, paused = _theme(api, ds, "vlm", status="On hold")
    _task(api, active["tasks"], "条件C")
    _task(api, active["tasks"], "自分の分", owner=OWNER_ME)
    _task(api, paused["tasks"], "止めたテーマ")
    tasks = store.tonight_tasks(5)
    assert [(t.title, t.theme, t.work) for t in tasks] == [("条件C", "amr-query", "作業: 条件Cを回す")]
    assert store.count_tonight_tasks() == 1


def test_update_task_appends_the_result_to_work_and_result(home):
    api, ds, store = home
    _, dbs = _theme(api, ds, "amr-query")
    task_id = _task(api, dbs["tasks"], "条件C")
    store.update_task(task_id, status=DONE, result="71%")
    task, = [t for t in store.awaiting_tasks() + store.tasks_due_within(date(2026, 10, 4), 3)
             if t.id == task_id] or [None]
    page = api.request("GET", f"/pages/{task_id}")
    text = "".join(t["plain_text"] for t in page["properties"]["Work & Result"]["rich_text"])
    assert text == "作業: 条件Cを回す\n結果: 71%"
    assert page["properties"]["Status"]["status"]["name"] == DONE


def test_due_and_waiting_tasks_cross_themes(home):
    api, ds, store = home
    _, a = _theme(api, ds, "amr-query")
    _, b = _theme(api, ds, "vlm")
    _task(api, a["tasks"], "今日", status="Not started", owner=OWNER_ME, due="2026-10-04")
    _task(api, b["tasks"], "待ち", status=WAITING)
    assert [t.title for t in store.tasks_due_on(date(2026, 10, 4))] == ["今日"]
    assert [(t.title, t.theme) for t in store.awaiting_tasks()] == [("待ち", "vlm")]


def test_a_theme_without_a_task_database_is_skipped(home):
    api, ds, store = home
    api.add_page(data_source=ds, properties={"Name": {"title": [{"text": {"content": "作りかけ"}}]},
                                             "Status": {"status": {"name": "In progress"}}})
    _, dbs = _theme(api, ds, "amr-query")
    _task(api, dbs["tasks"], "条件C")
    assert [t.theme for t in store.tonight_tasks(5)] == ["amr-query"]


def test_ensure_theme_uses_the_workspace_name_and_builds_the_page(home):
    api, ds, store = home
    assert store.ensure_theme("1-amr-query") is True
    assert store.ensure_theme("amr-query") is False
    (name, page_id), = store.active_themes()
    assert name == "amr-query"
    kinds = [b["type"] for b in _client(api).children(page_id)]
    assert kinds.count("heading_2") == 2 and kinds.count("child_database") == 2
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_notion_store.py -v`
Expected: FAIL（`ImportError: cannot import name 'DONE'`）

- [ ] **Step 3: 定数と Task の形を変える**

`notion_store.py` の import と `Task`・`Note` を次にする（`Note` は消す）。

```python
from kei_agent.storage.notion import (
    BLOCKS_PER_REQUEST,
    TASKS_TITLE,
    Notion,
    NotionError,
    append_blocks,
    create_theme_databases,
    gateway_notion,
    schema_problems,
    theme_page_blocks,
)
from kei_agent.workspaces.themes import theme_name as workspace_name

NOT_STARTED, TONIGHT, RUNNING, WAITING, DONE = "Not started", "Tonight", "Running", "Waiting", "Done"
OWNER_ME, OWNER_KEI = "Me", "Kei"
THEME_ACTIVE, THEME_ON_HOLD = "In progress", "On hold"
WORK_PREFIX, RESULT_PREFIX = "作業: ", "結果: "


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
```

`workspaces/themes.py` の `theme_name` が `storage` から読めない向き（`tests/test_layers.py`）なら、`notion_store.py` に次を置いて代わりに使う。

```python
_NUMBERED = re.compile(r"^\d+-")


def workspace_name(channel_name: str) -> str:
    """チャンネル名（1-amr-query）から作業場の名前（amr-query）を取る。"""
    return _NUMBERED.sub("", channel_name.lstrip("#"))
```

- [ ] **Step 4: テーマごとの DB を探す処理と Task の読み書きを書く**

`NotionStore` の「テーマ」「先行研究」「Task」「ノートとマイルストーン」の節を次に置き換える。

```python
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

    def ensure_theme(self, name: str) -> bool:
        """テーマの行がなければ作り、ページに見出しと Task・先行研究の DB を置く。作ったら True。"""
        name = workspace_name(name)
        if self.theme_page_id(name):
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
```

`__init__` に `self._task_sources: dict[str, str | None] = {}` を足し、`self._theme_names` を消す。`rich_text()` は 2000 字ごとに分けるので、Work & Result が長くなっても切れない。

- [ ] **Step 5: 窓口から定数を出す**

`src/kei_agent/api.py`:

```python
from kei_agent.storage.notion_store import (
    DONE, NOT_STARTED, OWNER_KEI, OWNER_ME, RUNNING, TONIGHT, WAITING, parse_slack_permalink, summarize,
)
```

`__all__` に `"DONE", "NOT_STARTED", "OWNER_KEI", "OWNER_ME", "RUNNING", "TONIGHT", "WAITING"` を足す。203 行目の説明を「研究ホーム（テーマと、テーマごとの Task・先行研究。kei_agent.storage.notion_store.NotionStore）」にする。

- [ ] **Step 6: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_notion_store.py tests/test_notion_setup.py tests/test_layers.py -v`
Expected: PASS

- [ ] **Step 7: コミット**

```bash
git add src/kei_agent/storage/notion_store.py src/kei_agent/api.py tests/test_notion_store.py
git commit -m "feat: read and write research tasks per theme"
```

---

### Task 3: 偽物と夜間の Task

**Files:**
- Modify: `src/kei_agent/testing/fakes.py:146-241`
- Modify: `modules/night/module.py`
- Modify: `src/kei_agent/conversation/assistant.py:366`
- Modify: `tests/test_schedule.py`（193-321、641、743-790 行目付近）

**Interfaces:**
- Consumes: Task 2 の `Task`・定数・`NotionStore` のメソッド名
- Produces: `FakeNotion.add_task(title, theme=None, status=TONIGHT, slack_url=None, body="", assignee=OWNER_KEI) -> Task`（`body` は Work & Result）、`FakeNotion.results: dict[str, str]`（最後に渡した `result`）、`FakeNotion.ensure_theme(name) -> bool`

- [ ] **Step 1: テストの値を新しい形にする**

`tests/test_schedule.py` で、`assistant.notion` の Task にかかわる値だけを置き換える（`test_runner.py` の「完了」は AI の返事なので触らない）。

```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path("tests/test_schedule.py")
t = p.read_text()
for old, new in (('"今夜やる"', "TONIGHT"), ('"確認待ち"', "WAITING"), ('"完了"', "DONE"), ('"未着手"', "NOT_STARTED")):
    t = t.replace(old, new)
p.write_text(t)
EOF
```

ファイルの先頭の import に `from kei_agent.api import DONE, NOT_STARTED, TONIGHT, WAITING` を足す。213 行目付近の「body=」を使うテストは、期待する頼みごとを `"作業: 2024年以降に絞る"` を含むことに直す。227・233 行目の「テーマなし」のテストは、新しい形では Task が必ずテーマの DB にあるので消す。

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_schedule.py -v -k "night or tonight or task"`
Expected: FAIL（`FakeNotion` がまだ日本語の値を使っている）

- [ ] **Step 3: 偽物を新しい形にする**

`src/kei_agent/testing/fakes.py` の `FakeNotion` を次にする（`Note`・`papers`・`notes`・`add_papers`・`paper_ids`・`create_night_task`・`notes_edited_since`・`upcoming_milestones`・`page_markdown` を消す）。

```python
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
        return [t for t in self.tasks.values() if t.due == day.isoformat()]

    def tasks_due_within(self, today, days):
        self._check()
        return []
```

import を `from kei_agent.storage.notion_store import OWNER_KEI, TONIGHT, WAITING, Task` にする。

- [ ] **Step 4: 夜間の Task を新しい形にする**

`modules/night/module.py` の import を `from kei_agent.api import DONE, RUNNING, WAITING, Core, NotionError, Request, clean_text, parse_slack_permalink, summarize` にし、`"確認待ち"`→`WAITING`、`"実行中"`→`RUNNING`、`"完了"`→`DONE` に置き換える。`_run_night_task` の前半と頼みごとを次にする。

```python
    async def _run_night_task(self, task, ids: dict[str, str]) -> dict:
        notion = self.core.notion
        info = {"title": task.title, "url": task.url, "theme": task.theme or ""}
        if task.theme not in ids:
            reason = f"テーマのチャンネル #{task.theme} に Kei Agent がいません"
            await asyncio.to_thread(notion.update_task, task.id, WAITING, reason)
            return {**info, "status": WAITING, "reason": reason}

        await asyncio.to_thread(notion.update_task, task.id, RUNNING)
        channel = ids[task.theme]
        # （ここから下のスレッドの決め方は今のまま）
```

頼みごとの文は次にする（優先度と本文の読み込みをやめ、Work & Result の「作業:」の部分を渡す）。

```python
        work = task.work.split("\n結果: ", 1)[0].removeprefix("作業: ").strip()
        text = (
            "[🌙 夜間の Task] 依頼者は寝ているので、その場で聞き返せません。"
            "判断が必要なところまで進めたら、最後の行を「❓ 確認:」で始めて止めてください。\n\n"
            f"タイトル: {task.title}\n期日: {task.due or '-'}\nNotion: {task.url}\n\n"
            f"## 作業\n\n{work or '（作業の中身なし）'}\n"
        )
```

`Request(channel, channel_name, ...)` の `channel_name` は `task.theme` にする。

- [ ] **Step 5: テーマの登録を新しい形にする**

`src/kei_agent/conversation/assistant.py:366`:

```python
            await asyncio.to_thread(self.notion.ensure_theme, ws.channel_name)
```

- [ ] **Step 6: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_schedule.py tests/test_notion_store.py -v`
Expected: PASS

- [ ] **Step 7: コミット**

```bash
git add src/kei_agent/testing/fakes.py modules/night/module.py src/kei_agent/conversation/assistant.py tests/test_schedule.py
git commit -m "feat: run night tasks from theme task databases"
```

---

### Task 4: Daily の材料

**Files:**
- Modify: `src/kei_agent/scheduling/digest.py:35,213-260`
- Modify: `modules/daily/texts.py:37-50`
- Modify: `tests/test_daily_module.py:85-86`、`tests/test_schedule.py:641` 付近

**Interfaces:**
- Consumes: Task 2・3 の定数と `NotionStore` のメソッド（`notes_edited_since`・`upcoming_milestones` は無い）
- Produces: `Digest._notion(since, now) -> tuple[list[str], list[str]]`（2つ目は共通ホームの振り返りだけ）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_daily_module.py` の 85-86 行目を新しい値にし、材料にマイルストーンが入らないことを足す。

```python
    assistant.notion.add_task("返事が要る", "vlm", status=WAITING)
    ...
    assert "マイルストーン" not in material
```

（`material` は、そのテストが Daily に渡した材料の文。既にある変数名に合わせる。）

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_daily_module.py -v`
Expected: FAIL（`AttributeError: 'FakeNotion' object has no attribute 'notes_edited_since'`）

- [ ] **Step 3: 材料を新しい形にする**

`digest.py` の `DONE = "完了"` を消し、`from kei_agent.storage.notion_store import DONE` にする。`_notion` を次にする。

```python
    async def _notion(self, since: float, now: float) -> tuple[list[str], list[str]]:
        """（テーマの Task の一覧, 前回以降の振り返り）。一覧は短く、今日のタスクの元になるので先に置く。"""
        notion = self.assistant.notion
        if notion is None:
            return ["## Notion", "", "- 設定されていない"], []
        today = datetime.fromtimestamp(now).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        try:
            reviews = []
            if self.assistant.hub is not None:
                reviews = [r for r in await asyncio.to_thread(
                    self.assistant.hub.reviews_edited_since, datetime.fromtimestamp(since)) if r.day != yesterday]
            today_tasks = await asyncio.to_thread(notion.tasks_due_on, today)
            awaiting = await asyncio.to_thread(notion.awaiting_tasks)
            due = await asyncio.to_thread(notion.tasks_due_within, today, 3)
            tonight = await asyncio.to_thread(notion.count_tonight_tasks)
        except NotionError as e:
            await self.assistant.notify_trouble(f"Daily の材料を Notion から読めませんでした: {e}")
            return ["## Notion", "", f"- 読めなかった: {e}"], []

        lines = ["## Notion: 今日が期日の Task（済みを含む）", ""]
        lines += [f"- {'済' if t.status == DONE else '未'} {t.title}（{t.theme}・{t.status}） {t.url}"
                  for t in today_tasks] or ["- なし"]
        lines += ["", "## Notion: 確認待ちの Task", ""]
        lines += [f"- {t.title}（{t.theme}） {t.url}" for t in awaiting] or ["- なし"]
        lines += ["", "## Notion: 期日が3日以内の Task", ""]
        lines += [f"- {t.due} {t.title}（{t.theme}・{t.status}・{t.owner or '-'}） {t.url}" for t in due] or ["- なし"]
        lines += ["", f"- 今夜やる Task: {tonight} 件"]

        bodies = ["## Notion: 前回以降の振り返り", ""]
        for r in reviews:
            bodies += [f"### 振り返り（{r.day or '-'}） {r.url}", "", _excerpt(r.body, NOTE_EXCERPT) or "（本文なし）", ""]
        if not reviews:
            bodies += ["- なし"]
        return lines, bodies
```

`modules/daily/texts.py` の 37 行目「材料（Notion のノートと Task を含む）」を「材料（Notion のテーマの Task を含む）」、47 行目「期日が近い Task とマイルストーン」を「期日が近い Task」、50 行目「振り返り・考察のノート」を「前日の振り返り」にする。

- [ ] **Step 4: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_daily_module.py tests/test_schedule.py tests/test_response_output.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/kei_agent/scheduling/digest.py modules/daily/texts.py tests/test_daily_module.py tests/test_schedule.py
git commit -m "feat: build the daily material from theme tasks"
```

---

### Task 5: 研究担当の skill・表示の文・文書

**Files:**
- Modify: `modules/research/plugin/skills/researching-literature/SKILL.md:18-40`
- Modify: `modules/research/plugin/skills/managing-wandb/SKILL.md:16`
- Modify: `src/kei_agent/operations/doctor.py:282`
- Modify: `docs/agents/research-agent.md`、`docs/architecture.md` の研究ホームの節（`git grep -n "ノート\|マイルストーン\|先行研究 DB" docs` で出る箇所）

- [ ] **Step 1: skill を新しい列にする**

`researching-literature/SKILL.md` の「保存済みかは…」の箇条と「保存する」の表を次にする。

```markdown
- 保存済みかは、そのテーマのページの「先行研究」DB を `kei-notion` の `query` で Link（arXiv は `https://arxiv.org/abs/<番号>`、バージョン番号は外す）を指定して確かめる。保存済みなら読み直さず、その行の Summary を使う

## 保存する

依頼に関係すると判断した論文だけを、そのテーマのページの「先行研究」DB に1本1行で入れる（`kei-notion` の `create_page`、親はその DB のデータソース。テーマのページは「テーマ」DB を Name で `query` して開く）。手元に `papers/` は作らない。

| 列 | 入れるもの |
|---|---|
| Title | 論文のタイトル |
| Summary | 何を問い、何をして、何が分かったか（2〜3文）と、テーマのページの「前提とキーワード」に照らした関係（1文） |
| Link | 論文のページの URL（同じ論文を二度入れないための鍵） |
| Status | Unread（読んだら Read、使うなら Use） |
```

「毎朝の新着は知識の担当が同じ DB に入れている（出どころ「毎朝の新着」）」の行は消す。`managing-wandb/SKILL.md:16` の「その実験のTaskの「結果」かノート（種類「考察」）に書く」を「その実験の Task の Work & Result に「結果: …」で書き足すか、テーマのページの進捗ログに日付付きで書く」にする。

- [ ] **Step 2: 表示の文と文書を直す**

`doctor.py:282` の `"研究ホーム（Task・ノート・先行研究）"` を `"研究ホーム（テーマと、テーマごとの Task・先行研究）"` にする。`docs/` の研究ホームの説明を、spec の「決めたこと」の形（テーマの列、テーマのページの順番、Task と先行研究の列）に書き換え、ノートとマイルストーンの説明を消す。

- [ ] **Step 3: 確かめる**

Run: `uv run python -m pytest && uvx ruff check .`
Expected: すべて PASS、ruff の指摘なし

- [ ] **Step 4: コミット**

```bash
git add modules/research/plugin/skills src/kei_agent/operations/doctor.py docs
git commit -m "docs: describe the theme-scoped research home"
```

---

### Task 6: Dot のプロンプト

**Files:**
- Modify: `docs/prompts/dot-daily.md`、`dot-night-tasks.md`、`dot-notices.md`、`dot-review.md`、`dot-literature.md`、`dot-custom-instructions.md`、`docs/prompts/README.md`

**前提:** PR #24 が main に入っていること。`git merge main` でこのブランチに取り込んでから始める。

- [ ] **Step 1: 定期実行の本文を書き換える**

各ファイルの ` ```text ` の中の本文（共通の決まりの後ろ）を、次の決まりに沿って書き換える。研究全体の Task と先行研究の `collection://` は消す。

- 共通の読み方（5件に入れる1行）: 「研究ホームの「テーマ」（collection://<テーマの data source>）で Status が In progress のテーマを順に開き、ページの中の「Task」「先行研究」の DB を読み書きする。テーマのページに DB が無いときは、そのテーマを飛ばして1行で知らせる」
- 朝の一覧と Daily: 締切は「各テーマの Task で Due が今日のもの（⏰ 時刻 締切: テーマ 題）」。確認待ちは Status が Waiting、期日が近いものは Due が3日以内で Status が Done でないもの、止まっているテーマは Status が On hold のもの
- 夜間の Task: 「各テーマの Task で Owner が Kei、Status が Tonight の行を、作った順に5件まで」。request は Work & Result の「作業:」の部分。状態は Running・Waiting・Done、結果は Work & Result に「結果: …」で書き足す。終わったら、そのテーマのページの進捗ログに日付付きで1〜3行を書き足す
- Kei Agent からの知らせ: 「各テーマの Task で Owner が Kei、Status が Running の行」。done→Done、needs_input→Waiting、failed→Waiting。結果は Work & Result に書き足す。ジョブの結果が出たら、そのテーマの進捗ログに1〜3行を書き足す
- 振り返り: 今日が Due の Task を各テーマから集める。最後に、その日に動きがあったテーマのページの進捗ログに「今日のまとめ（日付）」を1件書く。動きが無いテーマには書かない
- 先行研究の新着: 各テーマのページの「前提とキーワード」を読む（無ければそのテーマを飛ばす）。そのテーマの「先行研究」に Link で照合し、無ければ Title・Summary（要点2〜3文と関係1文）・Link・Status=Unread で作る。Summary は日本語

- [ ] **Step 2: 継続指示を書き換える**

`dot-custom-instructions.md` の「今夜やって」の行を次にする。

```text
- 「今夜やって」「夜にやっておいて」: そのテーマのページの「Task」DB に、Title・Status=Tonight・Owner=Kei・Work & Result=「作業: …」・Slack（その投稿のリンク）で1行作り、「🌙 今夜の Task にしたよ」と返す
```

「ほかの頼みごと」に次を足す。

```text
- 研究テーマで何かが進んだとき（Task が終わった、実験やジョブの結果が出た、テーマのチャンネルで方針が決まった）は、そのテーマのページの「進捗ログ」に日付付きで1〜3行を書き足す
```

「新しい研究テーマやプロジェクトのチャンネルができたら、create_workspace で作業場を作ってから受ける」は、そのまま（create_workspace がテーマの行とページの中身を作る）。

- [ ] **Step 3: 確かめて、コミット**

Run: `uv run python -m pytest tests/test_docs_contract.py -v`
Expected: PASS

```bash
git add docs/prompts
git commit -m "docs: point Dot prompts at theme-scoped research databases"
```

---

### Task 7: Notion の移し替えと切り替え（本物に触る。各段で利用者に確認）

**Files:** なし（Notion と Dot の設定の操作）

- [ ] **Step 1: 作る前に、何が変わるかを見せる**

Run: `uv run kei-agent-notion-setup`（`--apply` なし）
Expected: 「テーマ」に足す列（Status・Start・End・Duration）と、各テーマのページに置く DB の一覧が出る。利用者に見せて、進めてよいか確認する。

- [ ] **Step 2: テーマの DB の列を新しくする**

Notion の MCP（`notion-update-data-source`）で、テーマの DB の列を次の順で変える。

1. `名前` を `Name` に、`状態`（select）を消して `Status`（status: In progress / On hold / Done）を足す。各テーマの今の状態を写す（進行中→In progress、保留→On hold、完了→Done）
2. `Start`・`End`（date）と `Duration`（Task 1 の式）を足す
3. `目的`・`Slack`・`ディレクトリ`・`最終更新` を消す。消す前に、`目的` の中身を各テーマのページの「前提とキーワード」の下に書き写す

- [ ] **Step 3: テーマのページに見出しと DB を置く**

Run: `uv run kei-agent-notion-setup --apply`
Expected: 各テーマのページに「Task」「先行研究」の DB ができる。見出し（前提とキーワード・進捗ログ）が無いページには、Notion の MCP でページの先頭に足す。

- [ ] **Step 4: 古い行を移す**

Notion の MCP で、古い Task と先行研究の DB を全部読み（`notion-fetch` の view で全ページ）、各行の「テーマ」を見て、そのテーマの新しい DB に作る。

- Task: タイトル→Title、状態（未着手→Not started、今夜やる→Tonight、実行中→Running、確認待ち→Waiting、完了→Done）→Status、担当（自分→Me、Kei Agent→Kei）→Owner、期日→Due、本文を「作業: …」・結果を「結果: …」にして→Work & Result、Slack→Slack
- 先行研究: 名前→Title、要点＋この研究との関係→Summary、URL→Link、状態（未読→Unread、読んだ→Read、使う→Use）→Status
- テーマが付いていない行・複数のテーマが付いた Task は、移す先を利用者に聞く
- ノートは、テーマのページの下に同じ題のページを作って本文を写す。マイルストーンは、期日のあるものを Due 付きの Task（Owner=Me）にし、発表の予定などは利用者に予定カレンダーへ入れるか聞く

- [ ] **Step 5: 数と中身を照らし合わせる**

古い DB と新しい DB の行の数を、テーマごとに表にして利用者に見せる。題が古い DB にあって新しい DB に無いものを1件ずつ挙げる（0件になるまで直す）。

- [ ] **Step 6: Dot を切り替える**

PR をマージしたあと、Task 6 の全文を、今日と同じやり方で Kei に送る（「要約・言い換え・追記をせず、全文をそのまま保存して」）。保存後の照合の返事を確かめる。

- [ ] **Step 7: 古い DB を消す（利用者の確認のあと）**

古い Task・ノート・マイルストーン・先行研究の DB と、研究ホームの「自分の Task」「近いマイルストーン」の表について、消してよいかを利用者に確認する。よければ Notion の MCP でゴミ箱に入れる（30日間は戻せる）。`~/.local/state/kei-agent/notion.json` から `tasks`・`notes`・`milestones`・`papers` の鍵が消えていること（Step 3 の setup で書き直される）を確かめる。

- [ ] **Step 8: 翌日に確かめる**

翌朝の Daily、00:00 の夜間の Task、07:00 の先行研究の新着で、テーマのページの Task・先行研究・進捗ログが読み書きされていることを、Slack と Notion で確かめる。「今夜やって」で頼んだ Task が、そのテーマの Task に Tonight で入ることも確かめる。
