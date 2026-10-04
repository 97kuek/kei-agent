# 知識の Notion の作り直し Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 知識（記事・学び・助言・気づき）を共通ホームから分け、「知識ホーム（収集のページ＋Knowledge の DB）」の形にして、Kei Agent のコード・Dot のプロンプト・Notion の中身を切り替える。

**Architecture:** 知識ホームのページ ID は `agents.csv` の knowledge の行の `notion` 列に書き、`NotionConfig.knowledge_home` で読む。作りは共通ホームと同じ `src/kei_agent/storage/notion_hub_setup.py`（`HubSetup`）が受け持ち、知識ホームの中に「収集」のページと「Knowledge」の DB を作って、ID を `hub.json`（`HubState`）に控える。読み書きは `src/kei_agent/storage/notion_hub.py`（`HubStore`）の `add_article`・`add_learning`・`collect_settings` が行い、モジュール（`modules/knowledge/`・`modules/daily/`）は `core.hub` からそれを呼ぶ。

**Tech Stack:** Python 3（uv）、pytest、ruff、Notion API（ゲートウェイ経由、client は kei-agent）、Dot（ChatGPT）のプロンプト

**Spec:** `docs/superpowers/specs/2026-10-04-knowledge-notion-design.md`

## Global Constraints

- 知識ホーム: 共通ホーム（Keitaro Ueki）と同じ並び（ワークスペースの直下）。中に置くのは **収集**（ページ）と **Knowledge**（DB）の2つだけ。共通ホームの「リンク」に知識ホームへのリンクを置く
- 収集の書き方（「名前: キーワード、…」と、情報源の行）は今のまま（`COLLECT_DEFAULT`・`parse_collect` を変えない）
- Knowledge の列と順番: Title（title）→ Type（select）→ Summary（rich_text）→ Source（rich_text）→ Status（select）。この順に作り、表の表示もこの順にそろえる
- Type の選択肢: Article / Learning / Advice / Insight
- Status の選択肢: Unread / Read。Article にだけ付け、学びは空
- Source: 記事なら URL（照合キー。同じ URL の行があれば作らない）、学びなら誰から・どこで
- Summary: 記事なら要約2文、学びなら学んだことを1〜2文
- 学びの本文: 「場面・学んだこと・次にどう使うか」。最後に、学びが出た Slack のスレッドのリンク
- 日付の列は持たない（Notion のページの作成日時を使う）
- 種類の写し方: 学び→Learning、助言→Advice、気づき→Insight
- なくす DB: 読みもの・学びのノート。写さずに捨てる列: 読みものの出どころ・興味・日付、学びのノートの分野・日付
- 知識ホームのページ ID は `agents.csv` の knowledge の行の `notion` 列（`config.toml` には書かない。AGENTS.md）
- モジュールがコアに触れるのは `kei_agent.api` と `core` の窓口だけ。下の層から上の層を読み込まない（`tests/test_layers.py`）
- 柵（`src/kei_agent/execution/guard.py`・`config.example.toml`・`deploy/`）は変えない
- コメント・文書・Slack の文は日本語。文書には今の状態だけを書く
- 確かめる: `uv run python -m pytest` と `uvx ruff check .`（sandbox の外で）
- コミットの1行目は `feat:`・`fix:`・`docs:` などを頭に付けた英語の短い文
- 本物の Notion・Dot・`agents.csv`・`hub.json` に触れるのは Task 8 だけで、そのときも壊す・書き換える段の前に利用者の確認を取る

## Review Focus

- 前の `hub.json`（`reading_db_id`・`reading_ds_id`・`learning_db_id`・`learning_ds_id` の鍵がある）を新しいコードが読む → 共通ホームごと止めず（日別記録・時間記録はそのまま書ける）、Knowledge だけ未作成として扱う。`kei-agent-hub-setup` もその状態ファイルの上で動き、書き直した状態から古い鍵が消える（Task 2）
- 同じ URL の記事が Knowledge に既にある（Dot が先に保存した・前後に空白のある URL・`save_reading` の再送）→ 2行目を作らず、その行を使う。同じ文字列を Source に持つ学びの行とは取り違えない（Task 2、モジュール側は Task 4）
- 共通ホームに「収集」が残ったまま（知識ホームへ移す前に）`kei-agent-hub-setup --apply` を動かす → 知識ホームに既定の「収集」を作らず、何も書く前に、移すように書いた NotionError で止まる（Task 3）
- `agents.csv` の knowledge の行に `notion` が無い、または `hub.json` の知識ホームが設定と違う → 記事と学びの保存は「agents.csv の knowledge の行と setup を確かめて」と知らせて止まり、古い知識ホームには書かない。Daily・振り返りの日別記録は書ける（Task 2・Task 5）
- 振り返りの AI が想定外の種類（「教訓」、英語の「Advice」、「Article」、空）を返す → Type に新しい選択肢を作らず、英語の Learning / Advice / Insight はそのまま、ほかは Learning にする。学びには Status を付けない（Task 2）

---

## 順番の前提

- このプランは、研究の Notion のプラン（ブランチ `feat/research-notion`、`docs/superpowers/plans/2026-10-04-research-notion.md`）と授業の Notion のプラン（`docs/superpowers/plans/2026-10-04-course-notion.md`）が main に入ってから実行する。どれも `src/kei_agent/testing/fakes.py`・`src/kei_agent/storage/notion_hub_setup.py`・`tests/test_notion_hub.py`・`src/kei_agent/operations/doctor.py` を変えるため
- 始める前に `git fetch origin && git rebase origin/main` でこのブランチを main に載せ直し、main から `feat/knowledge-notion` を切って作業する。下の行番号は、このプランを書いた時点の main（`a5553cc`。`hands_server.py` だけは `origin/main` の `428951e`）のもの。ほかのプランで行がずれた箇所は、書いてある関数名・定数名・文で探す
- Task 2〜5 は1つの PR で入れる（Task 2 で `HubStore` の古いメソッドを消し、Task 4・5 でモジュールが新しいメソッドを使うまで、本物の動きはつながらない。テストはどのコミットでも通る）
- Task 7 の Knowledge の data source の ID は、Task 8 で Knowledge を作ってから書き入れる（決めたこと 4）

## 決めたこと（spec と今のコードが合わないところ）

1. **知識ホームの ID の置き場所:** spec は「共通ホームと同じく `agents.csv` の行で持つ」。共通ホーム専用の行（overview）に足す列は無いので、knowledge の行の `notion` 列を使う。今の `agents_table.parse` は、この列を `NotionConfig.homes["knowledge"]` にもう入れている（`tests/test_user_config.py` の `test_notion_homes_are_written_in_the_notion_column`）。足すのは読み口の `NotionConfig.knowledge_home` だけ。これでゲートウェイの client「knowledge」もこのホームだけに届くようになるが、knowledge のモジュールは AI を持たないので、使う人はいない
2. **知識ホームのページは手で作る:** Notion の接続（internal integration）はワークスペースの直下にページを作れない。知識ホームは Task 8 で利用者が作って共有し、`kei-agent-hub-setup --apply` は中の「収集」と「Knowledge」だけを作る
3. **Knowledge を作る順番:** spec の移し方は「Knowledge を作る → 行を移す → 確かめる → コードを切り替える」。新しいコードが main に入る前に Knowledge を作るため、Task 8 では Notion の MCP で Knowledge を spec の列の順に作り、コードを切り替えたあとの `kei-agent-hub-setup --apply` がそれを見つけて ID を控え、表の列の順番をそろえる（`HubSetup._ensure_source` は同名の DB があれば作らない）
4. **Dot のプロンプトの ID:** spec は「共通の決まりの中の data source の ID を Knowledge に差し替える」。今のプロンプトの共通の決まりにある ID は「Dot Work Log」だけで、読みものの ID は本文にある。共通の決まりは変えず、本文の ID を差し替える。Knowledge の ID は Task 8 で作るまで分からないので、Task 7 では `{{KNOWLEDGE_DS}}` と書き、Task 8 の Step 8 で本物の ID に置き換えてから Dot に送る
5. **解除:** spec の「解除は同じ URL の行をゴミ箱に入れる」どおり、`save_reading(saved=false)` は控えに保存先が無くても、Knowledge で同じ URL の Article を探してゴミ箱に入れる（Dot が入れた行でも）
6. **`collect_settings` の使い手:** 今の Kei Agent の中には `HubStore.collect_settings` を呼ぶところが無い（Dot が収集のページを直接読む）。spec どおり知識ホームの収集を読むように変え、テストで守る

## File Structure

| ファイル | 役目 | 変えること |
|---|---|---|
| `src/kei_agent/configuration/config.py` | 設定の形 | `NotionConfig.knowledge_home` を足す |
| `src/kei_agent/operations/doctor.py` | 点検 | knowledge の行の `notion` が空なら知らせる |
| `src/kei_agent/storage/notion_hub.py` | 共通ホームと知識ホームの読み書き | 読みもの・学びのノートの定義と処理を消し、Knowledge の定義、`add_article`・`find_article`・`add_learning` を足す。`collect_settings` は知識ホームを読む。`HubState` の鍵を替え、前の状態ファイルも読めるようにする |
| `src/kei_agent/storage/notion_hub_setup.py` | 共通ホームと知識ホームを作る | 共通ホームに読みもの・学びのノート・収集を作らない。知識ホームに収集と Knowledge を作り、列の順番をそろえる。収集を移す前なら止める |
| `modules/knowledge/module.py` | 旧配信分の保存 | `add_article` で Knowledge に入れ、解除は同じ URL の行をゴミ箱に入れる |
| `src/kei_agent/operations/hands_server.py` | MCP の道具の説明 | `save_reading`・`reading` の説明を Knowledge にする |
| `modules/daily/module.py`・`texts.py`・`module.toml` | 振り返りの学び | `add_learning` で Knowledge に入れる。文と JSON の形から分野を消す |
| `src/kei_agent/testing/fakes.py` | 偽物 | `FakeHub` を Knowledge の形にする |
| `docs/agents/knowledge-agent.md`・`docs/architecture.md`・`docs/using.md`・`docs/dots.md` | 文書 | 知識ホームの形にする |
| `docs/prompts/dot-reading.md`・`dot-review.md`・`dot-custom-instructions.md` | Dot のプロンプト | 収集は知識ホーム、保存と学びは Knowledge にする |
| `tests/test_user_config.py`・`tests/test_doctor.py`・`tests/test_notion_hub.py`・`tests/test_knowledge_module.py`・`tests/test_daily_module.py` | テスト | 新しい形にする |

---

### Task 1: 知識ホームの ID を設定から読む

**Files:**
- Modify: `src/kei_agent/configuration/config.py:143-160`（`NotionConfig`）
- Modify: `src/kei_agent/operations/doctor.py:281-283`（`check_notion`）
- Test: `tests/test_user_config.py:58-64`、`tests/test_doctor.py:161-168`

**Interfaces:**
- Consumes: なし
- Produces: `NotionConfig.knowledge_home -> str`（`agents.csv` の knowledge の行の `notion`。ハイフンを外した小文字。無ければ `""`）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_user_config.py` の `test_notion_homes_are_written_in_the_notion_column` の `assert config.notion.client_homes() == {"course": "aaaabbbb", "knowledge": "cccc"}` の次の行に足す。

```python
    assert config.notion.knowledge_home == "cccc"                       # 知識ホームは knowledge の行
```

`tests/test_doctor.py` の `test_notion_homes_and_tools` の、overview の行の assert の次に足す（`config` の `homes` は空）。

```python
    assert (WARN, "agents.csv の knowledge の行の notion が空（知識ホームの記事と学びを Notion に残さない）") in notion
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_user_config.py::test_notion_homes_are_written_in_the_notion_column tests/test_doctor.py::test_notion_homes_and_tools -v`
Expected: FAIL（`AttributeError: 'NotionConfig' object has no attribute 'knowledge_home'` と、doctor の assert の失敗）

- [ ] **Step 3: 実装する**

`src/kei_agent/configuration/config.py` の `NotionConfig.client_homes` の後ろに足す。

```python
    @property
    def knowledge_home(self) -> str:
        """知識ホーム（agents.csv の knowledge の行の notion）。収集のページと Knowledge の DB を置く。"""
        return self.homes.get("knowledge", "")
```

`src/kei_agent/operations/doctor.py` の `check_notion` の行の並びを次にする（研究のプランで research の行の文が変わっていれば、その文のまま knowledge の行だけ足す）。

```python
    for row, value, lost in (("overview", notion.hub_home, "Daily・振り返り・時間記録・予定カレンダー"),
                             ("research", notion.research_home, "研究ホーム（Task・ノート・先行研究）"),
                             ("course", notion.course_home, "授業ホーム（授業・課題）"),
                             ("knowledge", notion.knowledge_home, "知識ホームの記事と学び")):
```

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run python -m pytest tests/test_user_config.py tests/test_doctor.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/kei_agent/configuration/config.py src/kei_agent/operations/doctor.py tests/test_user_config.py tests/test_doctor.py
git commit -m "feat: read the knowledge home id from the knowledge row"
```

---

### Task 2: Knowledge の定義と読み書き（HubStore）

**Files:**
- Modify: `src/kei_agent/storage/notion_hub.py:10`（import）、`:56`（コメント）、`:124-149`（読みもの・学びのノートの定義）、`:162-180`（`HubState`）、`:203-205`（`schema_problems`）、`:353-421`（収集・読みもの・学びのノート）、`:547-562`（`load_hub`）
- Modify: `src/kei_agent/storage/notion_hub_setup.py:21-35`（import）、`:159`・`:290`・`:339`（状態ファイルの読み方）、`:260-267`（`inspect`）、`:335-336`・`:341-347`（`run`）、`:374-375`（`main`）
- Test: `tests/test_notion_hub.py`（`test_likes_are_written_to_the_reading_db` の 403-427 行目を消し、新しいテストを足す。`test_run_creates_schema_views_and_databases_only_once` の 228・239・270-279 行目を直す）

**Interfaces:**
- Consumes: `NotionConfig.knowledge_home`（Task 1）
- Produces（`kei_agent.storage.notion_hub`）:
  - 定数 `KNOWLEDGE_HOME_TITLE = "知識ホーム"`、`KNOWLEDGE_TITLE = "Knowledge"`、`ARTICLE = "Article"`、`LEARNING = "Learning"`、`KNOWLEDGE_TYPES = ("Article", "Learning", "Advice", "Insight")`、`UNREAD = "Unread"`、`KNOWLEDGE_STATUSES = ("Unread", "Read")`、`LEARNING_TYPES = {"学び": "Learning", "助言": "Advice", "気づき": "Insight"}`、`KNOWLEDGE_PROPERTIES: dict`（鍵の順は Title・Type・Summary・Source・Status）
  - `HubState` の鍵 `knowledge_home_id: str = ""`・`knowledge_db_id: str = ""`・`knowledge_ds_id: str = ""`（`reading_*`・`learning_*` は消す）
  - `HubState.from_json(raw: dict) -> HubState`（知らない鍵は読み飛ばす。dict でなければ `TypeError`）
  - `HubStore.has_knowledge_db -> bool`
  - `HubStore.find_article(url: str) -> str | None`
  - `HubStore.add_article(item: dict) -> str`（item は title・url・summary。同じ URL の Article があればその ID）
  - `HubStore.add_learning(item: dict, link: str = "") -> tuple[str, str]`（item は title・kind・source・scene・lesson・next。返り値は (ページ ID, URL)）
  - `HubStore.collect_settings() -> tuple[list[dict], list[str]]`（知識ホームの「収集」を読む）
  - `HubStore.trash_page(page_id: str) -> None`（今のまま）
  - 消すもの: `READING_TITLE`・`READING_STATES`・`READING_PROPERTIES`・`LEARNING_TITLE`・`LEARNING_FIELDS`・`LEARNING_KINDS`・`LEARNING_PROPERTIES`、`HubStore.has_reading_db`・`add_reading`・`has_learning_db`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_notion_hub.py` の import を次にする。

```python
import json
from dataclasses import replace
from datetime import date, datetime

import pytest
from fakes import check_notion_body

from kei_agent.storage.notion import NotionError
from kei_agent.storage.notion_hub import COLLECT_DEFAULT, HubState, HubStore, load_hub
from kei_agent.storage.notion_hub_setup import HubSetup
from kei_agent.storage.notion_store import markdown_to_blocks, plain_text
```

`test_likes_are_written_to_the_reading_db`（403-427 行目）を消し、その場所に次を足す。

```python
class FakeKnowledgeNotion:
    """Knowledge の DB と、ページの子だけの偽物。query は Type（select）と Source（rich_text）の equals だけを見る。"""

    def __init__(self):
        self.rows: list[dict] = []
        self.sent: list[tuple] = []
        self.blocks: dict[str, list[dict]] = {}

    def paginate(self, method, path, body):
        assert (method, path) == ("POST", "/data_sources/knowledge-ds/query")

        def holds(row, cond):
            prop = row["properties"].get(cond["property"], {})
            if "select" in cond:
                return (prop.get("select") or {}).get("name") == cond["select"]["equals"]
            return plain_text(prop.get("rich_text") or []) == cond["rich_text"]["equals"]

        return [row for row in self.rows if all(holds(row, cond) for cond in body["filter"]["and"])]

    def children(self, page_id):
        return list(self.blocks.get(page_id, []))

    def request(self, method, path, body=None):
        check_notion_body(body)
        self.sent.append((method, path, body))
        if (method, path) == ("POST", "/pages"):
            number = len(self.rows) + 1
            page = {"id": f"knowledge-{number}", "url": f"https://notion.so/knowledge-{number}",
                    "properties": body["properties"], "children": body.get("children") or []}
            self.rows.append(page)
            return page
        if method == "PATCH" and path.startswith("/pages/"):
            return {"id": path.removeprefix("/pages/")}
        raise AssertionError((method, path, body))


def knowledge_hub(notion=None) -> HubStore:
    return HubStore(notion or FakeKnowledgeNotion(), HubState(
        "home", "calendar-ds", "daily-ds", knowledge_home_id="knowledge-home",
        knowledge_db_id="knowledge-db", knowledge_ds_id="knowledge-ds"))


def test_articles_go_to_knowledge_once_per_url():
    """記事は Type=Article・Status=Unread で入る。同じ URL（前後の空白は除く）の Article があれば作らない。"""
    hub = knowledge_hub()
    assert hub.has_knowledge_db and not HubStore(hub.notion, HubState("home", "c", "d")).has_knowledge_db
    item = {"title": "LLM の話", "url": "https://zenn.dev/x", "summary": "要約の1文目。2文目。",
            "source": "Zenn, Inc.", "interests": ["AI"]}
    page_id = hub.add_article(item)
    assert hub.add_article({**item, "url": " https://zenn.dev/x ", "title": "別の題"}) == page_id
    (row,) = hub.notion.rows
    props = row["properties"]
    assert list(props) == ["Title", "Type", "Summary", "Source", "Status"]
    assert plain_text(props["Title"]["title"]) == "LLM の話"
    assert props["Type"] == {"select": {"name": "Article"}}
    assert plain_text(props["Summary"]["rich_text"]) == "要約の1文目。2文目。"
    assert plain_text(props["Source"]["rich_text"]) == "https://zenn.dev/x"
    assert props["Status"] == {"select": {"name": "Unread"}}
    assert hub.notion.sent[0][2]["parent"] == {"type": "data_source_id", "data_source_id": "knowledge-ds"}
    # 同じ文字列を Source に持つ学びの行とは取り違えない
    hub.add_learning({"title": "記事から学んだ", "kind": "学び", "source": "https://zenn.dev/y"})
    assert hub.add_article({**item, "url": "https://zenn.dev/y"}) not in (page_id, "knowledge-2")
    assert len(hub.notion.rows) == 3
    hub.trash_page(page_id)
    assert hub.notion.sent[-1] == ("PATCH", f"/pages/{page_id}", {"in_trash": True})


@pytest.mark.parametrize(("kind", "expected"), [
    ("学び", "Learning"), ("助言", "Advice"), ("気づき", "Insight"),
    ("Advice", "Advice"), ("教訓", "Learning"), ("Article", "Learning"), ("", "Learning")])
def test_learning_kinds_become_types_without_new_options_or_status(kind, expected):
    """学びは種類を Type にする（知らない種類は Learning）。Status は付けない。本文の最後に Slack のリンク。"""
    hub = knowledge_hub()
    page_id, url = hub.add_learning({
        "title": "レビューは結論から書く", "kind": kind, "field": "仕事", "source": "上司との1on1",
        "scene": "設計レビュー", "lesson": "先に結論を言うと議論が速い", "next": "次の資料で1行目に結論"},
        "https://slack.example/archives/C5/p1001")
    (row,) = hub.notion.rows
    props = row["properties"]
    assert (page_id, url) == ("knowledge-1", "https://notion.so/knowledge-1")
    assert list(props) == ["Title", "Type", "Summary", "Source"]
    assert props["Type"] == {"select": {"name": expected}}
    assert plain_text(props["Summary"]["rich_text"]) == "先に結論を言うと議論が速い"
    assert plain_text(props["Source"]["rich_text"]) == "上司との1on1"
    headings = [plain_text(b["heading_2"]["rich_text"]) for b in row["children"] if b["type"] == "heading_2"]
    assert headings == ["場面", "学んだこと", "次にどう使うか"]
    link = row["children"][-1]["paragraph"]["rich_text"][0]["text"]
    assert link["link"] == {"url": "https://slack.example/archives/C5/p1001"}


def test_learning_without_a_link_ends_with_the_next_step():
    hub = knowledge_hub()
    hub.add_learning({"title": "学び", "kind": "学び", "next": "次に試す"})
    assert hub.notion.rows[0]["children"][-1]["type"] == "paragraph"
    assert plain_text(hub.notion.rows[0]["children"][-1]["paragraph"]["rich_text"]) == "次に試す"


def test_collect_settings_come_from_the_knowledge_home_only():
    """収集は知識ホームのものだけを読む。共通ホームに残っていても読まない。"""
    notion = FakeKnowledgeNotion()
    notion.blocks["home"] = [{"id": "old-collect", "type": "child_page", "child_page": {"title": "収集"}}]
    hub = knowledge_hub(notion)
    with pytest.raises(NotionError, match="知識ホームに「収集」"):
        hub.collect_settings()
    notion.blocks["knowledge-home"] = [{"id": "collect", "type": "child_page", "child_page": {"title": "収集"}}]
    notion.blocks["collect"] = markdown_to_blocks(COLLECT_DEFAULT)
    interests, sources = hub.collect_settings()
    assert [i["name"] for i in interests] == ["AI・LLM・エージェント", "電子工作・ロボット", "Web・アプリ開発"]
    assert sources[0].startswith("zenn: llm") and "https://vercel.com/atom" in sources
    with pytest.raises(NotionError, match="agents.csv の knowledge"):
        HubStore(notion, HubState("home", "calendar-ds", "daily-ds")).collect_settings()


def test_schema_check_reads_the_knowledge_columns():
    class SchemaNotion:
        def request(self, method, path):
            if path == "/data_sources/knowledge-ds":
                return {"properties": {"Title": {"type": "title"}, "Type": {"type": "rich_text"},
                                       "Summary": {"type": "rich_text"}, "Source": {"type": "rich_text"},
                                       "Status": {"type": "select"}}}
            if path.startswith("/data_sources/"):
                return {"properties": {}}
            return {"id": "home"}

    hub = HubStore(SchemaNotion(), HubState("home", "calendar-ds", "daily-ds", knowledge_ds_id="knowledge-ds"))
    assert "Knowledgeの「Type」が select ではありません" in hub.schema_problems()


def _write_state(config, raw: dict):
    config.hub_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.hub_state_path.write_text(json.dumps(raw), encoding="utf-8")


def test_an_old_state_file_with_reading_ids_still_loads_the_hub(config, monkeypatch):
    """前の hub.json（読みもの・学びのノートの ID がある）でも共通ホームは止めない。Knowledge は未作成として扱う。"""
    monkeypatch.setattr("kei_agent.storage.notion_hub.gateway_notion", lambda *args, **kwargs: object())
    config = replace(config, notion=replace(config.notion, hub_home="home"))
    _write_state(config, {"home_id": "home", "calendar_ds_id": "calendar-ds", "daily_ds_id": "daily-ds",
                          "reading_db_id": "reading-db", "reading_ds_id": "reading-ds",
                          "learning_db_id": "learning-db", "learning_ds_id": "learning-ds"})
    hub = load_hub(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "test-token"})
    assert hub is not None and hub.state.daily_ds_id == "daily-ds" and not hub.has_knowledge_db
    _write_state(config, ["not", "a", "dict"])
    assert load_hub(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "test-token"}) is None


@pytest.mark.parametrize(("configured", "kept"), [("knowledge-home", True), ("KNOWLEDGEHOME", True),
                                                  ("other-home", False), ("", False)])
def test_knowledge_is_used_only_for_the_configured_home(config, monkeypatch, configured, kept):
    """hub.json の知識ホームが agents.csv と違えば（未設定も）、setup をやり直すまで Knowledge には書かない。"""
    monkeypatch.setattr("kei_agent.storage.notion_hub.gateway_notion", lambda *args, **kwargs: object())
    homes = {"knowledge": configured} if configured else {}
    config = replace(config, notion=replace(config.notion, hub_home="home", homes=homes))
    _write_state(config, HubState("home", "calendar-ds", "daily-ds", knowledge_home_id="knowledge-home",
                                  knowledge_db_id="knowledge-db", knowledge_ds_id="knowledge-ds").__dict__)
    hub = load_hub(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "test-token"})
    assert hub is not None and hub.state.daily_ds_id == "daily-ds" and hub.has_knowledge_db is kept
    if not kept:
        with pytest.raises(NotionError, match="agents.csv の knowledge"):
            hub.add_article({"title": "x", "url": "https://zenn.dev/x"})
        with pytest.raises(NotionError, match="agents.csv の knowledge"):
            hub.add_learning({"title": "x"})


def test_setup_reads_an_old_state_file_and_drops_the_old_ids(fake_notion, tmp_path):
    """前の hub.json でも setup は止まらない。書き直した状態から古い鍵が消え、読みもの・学びのノートは作らない。"""
    (tmp_path / "hub.json").write_text(json.dumps({
        "home_id": "home", "calendar_ds_id": "calendar-ds", "daily_ds_id": "",
        "reading_db_id": "reading-db", "reading_ds_id": "reading-ds",
        "learning_db_id": "learning-db", "learning_ds_id": "learning-ds"}), encoding="utf-8")
    setup(fake_notion, tmp_path).run()
    saved = json.loads((tmp_path / "hub.json").read_text())
    assert not any(key.startswith(("reading_", "learning_")) for key in saved)
    titles = [b["child_database"]["title"] for b in fake_notion.blocks["home"] if b["type"] == "child_database"]
    assert not {"読みもの", "学びのノート"} & set(titles)
```

`test_run_creates_schema_views_and_databases_only_once` を直す。228 行目の docstring を `"""二度目の setup では何も書かない。日別・時間・集め方のページは1つずつで、正本の親は動かさない。"""` に、239 行目を次にする。

```python
    assert titles.count("日別記録") == 1 and not {"読みもの", "学びのノート"} & set(titles)
```

270-279 行目（「👍 した記事の入れ先」と「集め方のページは…」の2つの塊）を消す（収集の読み返しは Task 3 で知識ホームのテストに移す）。

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_notion_hub.py -v`
Expected: FAIL（`ImportError` は出ず、`TypeError: HubState.__init__() got an unexpected keyword argument 'knowledge_home_id'` などで新しいテストが落ちる）

- [ ] **Step 3: notion_hub.py の定義を書き換える**

10 行目を `from dataclasses import dataclass, fields, replace` にし、56 行目のコメントを `# 知識の担当が毎朝読む設定のページ（知識ホームの子ページ）。書き方は COLLECT_NOTE` にする。

124-149 行目（`# 朝の読みもので 👍 した記事…` から `LEARNING_PROPERTIES` の終わりまで）を次に置き換える。

```python
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
```

`HubState`（162-180 行目）の `reading_db_id` から `learning_ds_id` までの4行を次に置き換える。

```python
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
```

`schema_problems` の 203-205 行目を次にする。

```python
        for label, ds_id, spec_of in (("時間記録", self.state.time_ds_id, TIME_PROPERTIES),
                                      (KNOWLEDGE_TITLE, self.state.knowledge_ds_id, KNOWLEDGE_PROPERTIES)):
```

- [ ] **Step 4: notion_hub.py の読み書きを書き換える**

353-421 行目（`# 収集` から `add_learning` の終わりまで。`trash_page` は残す）を次に置き換える。

```python
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
        rows = self.notion.paginate("POST", f"/data_sources/{self._knowledge_ds()}/query", {
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
```

`load_hub`（547-562 行目）の `try` から最後までを次にする。

```python
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
```

- [ ] **Step 5: notion_hub_setup.py を新しい HubState に合わせる**

import（21-35 行目）から `LEARNING_PROPERTIES`・`LEARNING_TITLE`・`READING_PROPERTIES`・`READING_TITLE` を消す。

状態ファイルを読む3か所（159・290・339 行目）の `HubState(**json.loads(self.state_path.read_text(encoding="utf-8")))` を `HubState.from_json(json.loads(self.state_path.read_text(encoding="utf-8")))` にする。

`inspect` の `reading = …`・`learning = …` の2行（260-261）と、戻り値の `f"読みもの: …"`・`f"学びのノート: …"` の2行（266-267）を消す。

`run` の `reading = self._ensure_source(READING_TITLE, …)`・`learning = self._ensure_source(LEARNING_TITLE, …)` の2行（335-336）を消し、`state = HubState(…)`（341-347 行目）を次にする。

```python
        state = HubState(self.home_id, calendar.data_source_id, daily.data_source_id,
                         calendar.database_id, daily.database_id,
                         tasks.data_source_id, assignments.data_source_id,
                         views["研究 Task"]["id"], views["授業課題"]["id"], daily_view_id,
                         time_source.database_id, time_source.data_source_id, chart_id)
```

`main` の表示（374-375 行目）を次にする。

```python
            print(f"適用完了: 日別記録 {state.daily_ds_id}、カレンダー {state.calendar_ds_id}、"
                  f"時間記録 {state.time_ds_id}")
```

- [ ] **Step 6: 通ることを確かめる**

Run: `uv run python -m pytest tests/test_notion_hub.py -v && uvx ruff check src/kei_agent/storage tests/test_notion_hub.py`
Expected: PASS、ruff の指摘なし

- [ ] **Step 7: 全体を確かめる**

Run: `uv run python -m pytest`
Expected: PASS（モジュールのテストは `FakeHub` を使うので、この時点では変わらない）

- [ ] **Step 8: コミット**

```bash
git add src/kei_agent/storage/notion_hub.py src/kei_agent/storage/notion_hub_setup.py tests/test_notion_hub.py
git commit -m "feat: store articles and learnings in the Knowledge database"
```

---

### Task 3: 知識ホームを作る（HubSetup）

**Files:**
- Modify: `src/kei_agent/storage/notion_hub_setup.py:21-35`（import）、`:73-82`（`__init__`）、`:192-220`（`_owned_source`・`_ensure_source`）、`:257-271`（`inspect`）、`:273-349`（`run`）、`:367-375`（`main`）
- Test: `tests/test_notion_hub.py:69-196`（`FakeHubNotion`・`setup`）と新しいテスト、`:227-241`（共通ホームの収集の assert）

**Interfaces:**
- Consumes: `KNOWLEDGE_HOME_TITLE`・`KNOWLEDGE_TITLE`・`KNOWLEDGE_PROPERTIES`・`COLLECT_TITLE`・`COLLECT_DEFAULT`・`HubState`（`knowledge_*` の鍵）・`HubStore.collect_settings`（Task 2）、`NotionConfig.knowledge_home`（Task 1）
- Produces:
  - `HubSetup(notion, home_id, state_path, *, research_home_id: str, course_home_id: str, knowledge_home_id: str = "")`
  - `HubSetup._collect_page(parent: str) -> str | None`
  - `HubSetup._knowledge_preflight() -> None`（何も書かずに確かめる。収集が共通ホームだけにあれば `NotionError`）
  - `HubSetup._knowledge() -> _Source | None`（知識ホームが未設定なら `None` と warnings）
  - `HubSetup._order_view(title: str, source: _Source, names: tuple[str, ...]) -> None`（失敗は warnings）
  - `_owned_source(title, properties, parent: str = "")`・`_ensure_source(title, properties, icon: str = "", parent: str = "")`（parent が空なら共通ホーム）
  - `run()` の `HubState` に `knowledge_home_id`・`knowledge_db_id`・`knowledge_ds_id` が入る

- [ ] **Step 1: 偽物を知識ホームに広げる**

`tests/test_notion_hub.py` の `FakeHubNotion.__init__` の `self.pages` に `"knowledge-home": {"id": "knowledge-home", "parent": {"type": "workspace"}},` を、`self.blocks` に `"knowledge-home": [],` を足し、最後に次を足す。

```python
        self.knowledge_view = {"id": "knowledge-view", "type": "table", "configuration": None}
```

`request` の GET の `/views?` の分岐を次にする（Knowledge の表のビューは1つ）。

```python
            if path.startswith("/views?"):
                if path == "/views?database_id=daily-db":
                    return {"results": [{"id": view_id} for view_id in self.daily_view_ids]}
                if path == "/views?database_id=knowledge-db":
                    return {"results": [{"id": "knowledge-view"}]}
                return {"results": list(self.views)}
            if path.startswith("/views/"):
                if path == "/views/daily-default-view":
                    return self.daily_view
                if path == "/views/knowledge-view":
                    return self.knowledge_view
```

`POST /databases` の分岐を、親のページを本文から読むようにする。

```python
        if method == "POST" and path == "/databases":
            title = body["title"][0]["text"]["content"]
            parent = body["parent"]["page_id"]
            prefix = {"日別記録": "daily", "時間記録": "time", "Knowledge": "knowledge"}[title]
            db_id, ds_id = f"{prefix}-db", f"{prefix}-ds"
            self.blocks[parent].append(self.child_db(db_id, title))
            self.databases[db_id] = {"id": db_id, "parent": {"type": "page_id", "page_id": parent},
                                     "data_sources": [{"id": ds_id}]}
            self.sources[ds_id] = {"id": ds_id, "properties": {
                name: {"id": name, "type": next(iter(config))}
                for name, config in body["initial_data_source"]["properties"].items()}}
            return self.databases[db_id]
```

`PATCH /views/daily-default-view` の分岐の次に足す。

```python
        if (method, path) == ("PATCH", "/views/knowledge-view"):
            self.knowledge_view.update(body)
            return self.knowledge_view
```

子ページを作る分岐を、どのページの下にも作れるようにする。

```python
        if method == "POST" and path == "/pages" and body.get("parent", {}).get("type") == "page_id":
            parent = body["parent"]["page_id"]
            page_id = f"child-page-{len(self.blocks)}"
            title = body["properties"]["title"]["title"][0]["text"]["content"]
            self.blocks.setdefault(parent, []).append(
                {"id": page_id, "type": "child_page", "child_page": {"title": title}})
            self.blocks[page_id] = list(body.get("children") or [])
            return {"id": page_id}
```

`setup` を次にする。

```python
def setup(fake_notion, tmp_path):
    return HubSetup(fake_notion, "home", tmp_path / "hub.json",
                    research_home_id="research-home", course_home_id="course-home",
                    knowledge_home_id="knowledge-home")
```

- [ ] **Step 2: 失敗するテストを書く**

`test_run_creates_schema_views_and_databases_only_once` の、収集を数える assert（240-241 行目）を次にする。

```python
    assert COLLECT_TITLE not in [b["child_page"]["title"] for b in fake_notion.blocks["home"]
                                 if b["type"] == "child_page"]
```

`test_inspect_reports_existing_sources_without_writes` の後ろに足す。

```python
def test_setup_makes_the_knowledge_home_with_collect_and_knowledge_in_column_order(fake_notion, tmp_path):
    """知識ホームには「収集」と Knowledge だけを置く。列は Title → Type → Summary → Source → Status の順で、表もその順。"""
    hub_setup = setup(fake_notion, tmp_path)
    state = hub_setup.run()
    assert (state.knowledge_home_id, state.knowledge_db_id, state.knowledge_ds_id) == (
        "knowledge-home", "knowledge-db", "knowledge-ds")
    kinds = [(b["type"], b.get("child_page", b.get("child_database", {})).get("title"))
             for b in fake_notion.blocks["knowledge-home"]]
    assert kinds == [("child_page", "収集"), ("child_database", "Knowledge")]
    home_titles = {b.get("child_page", b.get("child_database", {})).get("title") for b in fake_notion.blocks["home"]}
    assert not {"収集", "読みもの", "学びのノート", "Knowledge"} & home_titles
    props = fake_notion.sources["knowledge-ds"]["properties"]
    assert list(props) == ["Title", "Type", "Summary", "Source", "Status"]
    assert {name: prop["type"] for name, prop in props.items()} == {
        "Title": "title", "Type": "select", "Summary": "rich_text", "Source": "rich_text", "Status": "select"}
    created = next(body for method, path, body in fake_notion.writes
                   if (method, path) == ("POST", "/databases") and body["title"][0]["text"]["content"] == "Knowledge")
    assert created["parent"] == {"type": "page_id", "page_id": "knowledge-home"}
    options = created["initial_data_source"]["properties"]
    assert [o["name"] for o in options["Type"]["select"]["options"]] == ["Article", "Learning", "Advice", "Insight"]
    assert [o["name"] for o in options["Status"]["select"]["options"]] == ["Unread", "Read"]
    shown = [p["property_id"] for p in fake_notion.knowledge_view["configuration"]["properties"] if p["visible"]]
    assert shown == ["Title", "Type", "Summary", "Source", "Status"]
    assert json.loads((tmp_path / "hub.json").read_text())["knowledge_ds_id"] == "knowledge-ds"
    # 収集は知識ホームから読み返せる
    interests, sources = HubStore(fake_notion, state).collect_settings()
    assert [i["name"] for i in interests] == ["AI・LLM・エージェント", "電子工作・ロボット", "Web・アプリ開発"]
    assert sources[0].startswith("zenn: llm") and "https://vercel.com/atom" in sources
    # 二度目は何も書かない
    writes = len(fake_notion.writes)
    fake_notion.hide_request_fields = True
    assert hub_setup.run() == state and len(fake_notion.writes) == writes


def test_setup_stops_before_writing_while_collect_is_still_in_the_common_home(fake_notion, tmp_path):
    """収集を知識ホームへ移す前に --apply すると、既定の収集を作らずに、何も書く前に止まる。移したあとは通る。"""
    fake_notion.blocks["home"].append({"id": "old-collect", "type": "child_page", "child_page": {"title": "収集"}})
    with pytest.raises(NotionError, match="共通ホームに「収集」が残っています"):
        setup(fake_notion, tmp_path).run()
    with pytest.raises(NotionError, match="共通ホームに「収集」が残っています"):
        setup(fake_notion, tmp_path).inspect()
    assert fake_notion.writes == []
    fake_notion.blocks["home"].pop()
    fake_notion.blocks["knowledge-home"].append(
        {"id": "moved-collect", "type": "child_page", "child_page": {"title": "収集"}})
    setup(fake_notion, tmp_path).run()
    assert [b["id"] for b in fake_notion.blocks["knowledge-home"] if b["type"] == "child_page"] == ["moved-collect"]


def test_setup_adopts_a_knowledge_database_made_by_hand(fake_notion, tmp_path):
    """移し替えで先に手で作った Knowledge は作り直さず、ID を控えて表の列だけそろえる。"""
    fake_notion.blocks["knowledge-home"].append(fake_notion.child_db("knowledge-db", "Knowledge"))
    fake_notion.databases["knowledge-db"] = {"id": "knowledge-db", "parent": {"type": "page_id", "page_id": "knowledge-home"},
                                             "data_sources": [{"id": "knowledge-ds"}]}
    fake_notion.sources["knowledge-ds"] = fake_notion.ds("knowledge-ds", {
        "Title": "title", "Type": "select", "Summary": "rich_text", "Source": "rich_text", "Status": "select"})
    fake_notion.knowledge_view["configuration"] = {"properties": [
        {"property_id": name, "visible": True} for name in ("Title", "Status", "Type", "Source", "Summary")]}
    state = setup(fake_notion, tmp_path).run()
    assert state.knowledge_ds_id == "knowledge-ds"
    assert not any(path == "/databases" and body["title"][0]["text"]["content"] == "Knowledge"
                   for method, path, body in fake_notion.writes if method == "POST")
    shown = [p["property_id"] for p in fake_notion.knowledge_view["configuration"]["properties"] if p["visible"]]
    assert shown == ["Title", "Type", "Summary", "Source", "Status"]


def test_setup_without_a_knowledge_home_leaves_knowledge_empty_and_says_so(fake_notion, tmp_path):
    hub_setup = HubSetup(fake_notion, "home", tmp_path / "hub.json",
                         research_home_id="research-home", course_home_id="course-home")
    state = hub_setup.run()
    assert (state.knowledge_home_id, state.knowledge_ds_id) == ("", "")
    assert fake_notion.blocks["knowledge-home"] == []
    assert any("knowledge の行" in warning for warning in hub_setup.warnings)
    assert any(line.startswith("知識ホーム: 未設定") for line in hub_setup.inspect())


def test_column_order_failure_does_not_stop_setup(fake_notion, tmp_path):
    fake_notion.knowledge_view["type"] = "board"
    hub_setup = setup(fake_notion, tmp_path)
    assert hub_setup.run().knowledge_ds_id == "knowledge-ds"
    assert any("列の順番" in warning for warning in hub_setup.warnings)
```

- [ ] **Step 3: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_notion_hub.py -v`
Expected: FAIL（`TypeError: HubSetup.__init__() got an unexpected keyword argument 'knowledge_home_id'`）

- [ ] **Step 4: HubSetup を書き換える**

import に `KNOWLEDGE_HOME_TITLE`・`KNOWLEDGE_PROPERTIES`・`KNOWLEDGE_TITLE` を足す（`COLLECT_DEFAULT`・`COLLECT_TITLE` は残す）。

`__init__` を次にする。

```python
    def __init__(self, notion: Notion, home_id: str, state_path: Path, *,
                 research_home_id: str,
                 course_home_id: str,
                 knowledge_home_id: str = ""):
        self.notion = notion
        self.home_id = home_id
        self.state_path = state_path
        self.research_home_id = research_home_id
        self.course_home_id = course_home_id
        # 知識ホーム（agents.csv の knowledge の行の notion）。空なら収集と Knowledge を作らない
        self.knowledge_home_id = knowledge_home_id
        # 止めるほどではない失敗（グラフのビューなど）。呼び出し側が表示する
        self.warnings: list[str] = []
```

`_owned_source` と `_ensure_source` を次にする。

```python
    def _owned_source(self, title: str, properties: dict, parent: str = "") -> _Source | None:
        """Kei Agent が書く DB（共通ホームの時間記録、知識ホームの Knowledge）。無ければ None。"""
        found = self._source(parent or self.home_id, (title,), title, required={}, optional=True)
        if found:
            for name, spec in properties.items():
                actual = found.properties.get(name)
                if actual and actual.get("type") != next(iter(spec)):
                    raise NotionError(f"{title}の「{name}」の型が異なります")
        return found

    def _ensure_source(self, title: str, properties: dict, icon: str = "", parent: str = "") -> _Source:
        """Kei Agent が書く DB を用意する。無ければ properties の順に列を作り、足りない列を足す。"""
        parent = parent or self.home_id
        source = self._owned_source(title, properties, parent)
        if source is None:
            body = {"parent": {"type": "page_id", "page_id": parent},
                    "title": [{"text": {"content": title}}],
                    "initial_data_source": {"properties": properties}}
            if icon:
                body["icon"] = {"type": "emoji", "emoji": icon}
            created = self.notion.request("POST", "/databases", body)
            source = self._owned_source(title, properties, parent)
            if source is None or source.database_id != created["id"]:
                raise NotionError(f"作成した{title}を再確認できません")
        missing = {n: p for n, p in properties.items() if n not in source.properties}
        if missing:
            self.notion.request("PATCH", f"/data_sources/{source.data_source_id}", {"properties": missing})
            source = self._owned_source(title, properties, parent)
            assert source is not None
        return source
```

`_time_chart` の後ろに、知識ホームの処理を足す。

```python
    def _collect_page(self, parent: str) -> str | None:
        """parent の直下の「収集」ページの ID。無ければ None。"""
        return next((block["id"] for block in self.notion.children(parent)
                     if block.get("type") == "child_page" and block["child_page"].get("title") == COLLECT_TITLE), None)

    def _knowledge_preflight(self) -> None:
        """知識ホームを読むだけで確かめる。共通ホームに「収集」が残っていて知識ホームに無ければ、移すまで止める。"""
        if not self.knowledge_home_id:
            return
        self._page(self.knowledge_home_id, KNOWLEDGE_HOME_TITLE)
        if self._collect_page(self.home_id) and not self._collect_page(self.knowledge_home_id):
            raise NotionError(f"共通ホームに「{COLLECT_TITLE}」が残っています。{KNOWLEDGE_HOME_TITLE}へ移してから、"
                              "もう一度実行してください（既定の収集で上書きしないため）")
        self._owned_source(KNOWLEDGE_TITLE, KNOWLEDGE_PROPERTIES, self.knowledge_home_id)

    def _knowledge(self) -> _Source | None:
        """知識ホームに「収集」のページと Knowledge の DB を用意する。知識ホームが未設定なら何もしない。"""
        if not self.knowledge_home_id:
            self.warnings.append("agents.csv の knowledge の行に notion が無いので、知識ホームの収集と "
                                 f"{KNOWLEDGE_TITLE} を作っていません")
            return None
        if not self._collect_page(self.knowledge_home_id):
            # 中身は利用者が直していくので、作るのは無いときだけ
            self.notion.request("POST", "/pages", {
                "parent": {"type": "page_id", "page_id": self.knowledge_home_id},
                "icon": {"type": "emoji", "emoji": "🧺"},
                "properties": {"title": {"title": [{"text": {"content": COLLECT_TITLE}}]}},
                "children": markdown_to_blocks(COLLECT_DEFAULT),
            })
        source = self._ensure_source(KNOWLEDGE_TITLE, KNOWLEDGE_PROPERTIES, icon="📚", parent=self.knowledge_home_id)
        self._order_view(KNOWLEDGE_TITLE, source, tuple(KNOWLEDGE_PROPERTIES))
        return source

    def _order_view(self, title: str, source: _Source, names: tuple[str, ...]) -> None:
        """DB の表の列を names の順にし、ほかの列はその右に置く。そろっていれば書かない。できなくても止めない（warnings）。"""
        try:
            response = self.notion.request("GET", f"/views?database_id={source.database_id}")
            ids = [view["id"] for view in response.get("results", [])]
            if len(ids) != 1:
                raise NotionError(f"表のビューが {len(ids)} 個あります")
            view = self.notion.request("GET", f"/views/{ids[0]}")
            if view.get("type") != "table":
                raise NotionError("ビューが表ではありません")
            order = [*names, *(name for name in source.properties if name not in names)]
            wanted = [source.properties[name]["id"] for name in order]
            actual = [prop["property_id"] for prop in (view.get("configuration") or {}).get("properties", [])
                      if prop.get("visible")]
            if actual != wanted:
                self.notion.request("PATCH", f"/views/{ids[0]}", {"configuration": {
                    "type": "table", "properties": [{"property_id": pid, "visible": True} for pid in wanted]}})
        except (NotionError, KeyError, TypeError) as e:
            message = f"{title}の表の列の順番をそろえられませんでした（Notion の画面でそろえてください）: {e}"
            log.warning(message)
            self.warnings.append(message)
```

`inspect` を次にする。

```python
    def inspect(self) -> list[str]:
        calendar, daily, tasks, assignments, views = self._preflight()
        self._knowledge_preflight()
        time_source = self._owned_source(TIME_TITLE, TIME_PROPERTIES)
        knowledge = (self._owned_source(KNOWLEDGE_TITLE, KNOWLEDGE_PROPERTIES, self.knowledge_home_id)
                     if self.knowledge_home_id else None)
        return [
            f"今月の予定／予定カレンダー: {calendar.database_id}",
            f"日別記録: {daily.database_id if daily else '未作成'}",
            f"時間記録: {time_source.database_id if time_source else '未作成'}",
            f"研究 Task: {tasks.database_id}",
            f"授業課題: {assignments.database_id}",
            f"親ページのリンクドビュー: {', '.join(views) if views else '未作成'}",
            f"{KNOWLEDGE_HOME_TITLE}: {self.knowledge_home_id or '未設定（agents.csv の knowledge の行の notion）'}",
            f"{KNOWLEDGE_TITLE}: {knowledge.database_id if knowledge else '未作成（--apply で作る）'}",
        ]
```

`run` を3か所直す。先頭の `calendar, daily, tasks, assignments, views = self._preflight()` の次の行に `self._knowledge_preflight()` を足す。共通ホームに収集を作る塊（325-333 行目、`if not any(block.get("type") == "child_page" … COLLECT_TITLE …` から `})` まで）を消す。`time_source = self._ensure_source(TIME_TITLE, TIME_PROPERTIES)` の次の行に `knowledge = self._knowledge()` を足し、`state = HubState(…)` を次にする。

```python
        state = HubState(self.home_id, calendar.data_source_id, daily.data_source_id,
                         calendar.database_id, daily.database_id,
                         tasks.data_source_id, assignments.data_source_id,
                         views["研究 Task"]["id"], views["授業課題"]["id"], daily_view_id,
                         time_source.database_id, time_source.data_source_id, chart_id,
                         knowledge_home_id=self.knowledge_home_id if knowledge else "",
                         knowledge_db_id=knowledge.database_id if knowledge else "",
                         knowledge_ds_id=knowledge.data_source_id if knowledge else "")
```

`main` の `HubSetup(…)` と表示を次にする。

```python
    setup = HubSetup(notion, config.notion.hub_home, config.hub_state_path,
                     research_home_id=config.notion.research_home, course_home_id=config.notion.course_home,
                     knowledge_home_id=config.notion.knowledge_home)
```

```python
            print(f"適用完了: 日別記録 {state.daily_ds_id}、カレンダー {state.calendar_ds_id}、"
                  f"時間記録 {state.time_ds_id}、Knowledge {state.knowledge_ds_id or '未作成'}")
```

- [ ] **Step 5: 通ることを確かめる**

Run: `uv run python -m pytest tests/test_notion_hub.py -v && uvx ruff check src/kei_agent/storage tests/test_notion_hub.py`
Expected: PASS、ruff の指摘なし

- [ ] **Step 6: 全体を確かめる**

Run: `uv run python -m pytest`
Expected: PASS

- [ ] **Step 7: コミット**

```bash
git add src/kei_agent/storage/notion_hub_setup.py tests/test_notion_hub.py
git commit -m "feat: set up the knowledge home with collect and Knowledge"
```

---

### Task 4: 旧配信分の保存を Knowledge にする（knowledge モジュール）

**Files:**
- Modify: `modules/knowledge/module.py:36-102`（`head_action`・`_save`・`_forget`）
- Modify: `src/kei_agent/testing/fakes.py:616-662`（`FakeHub` の読みもの）
- Modify: `src/kei_agent/operations/hands_server.py:128-130`・`:180-181`（origin/main の行。`save_reading`・`reading` の説明）
- Test: `tests/test_knowledge_module.py`

**Interfaces:**
- Consumes: `HubStore.has_knowledge_db`・`find_article(url) -> str | None`・`add_article(item) -> str`・`trash_page(page_id)`（Task 2）
- Produces:
  - `FakeHub.has_knowledge_db = True`、`FakeHub.knowledge: dict[str, dict]`（ページ ID → `{"type": "Article", "url": str, "item": dict}`。学びは Task 5 で `{"type": "Learning", "item": dict, "link": str}`）
  - `FakeHub.find_article(url: str) -> str | None`（ゴミ箱に入れた行は数えない）、`FakeHub.add_article(item: dict) -> str`（ID は `knowledge-<番号>`）
  - 消すもの: `FakeHub.has_reading_db`・`readings`・`add_reading`
  - `Module._forget(url: str, posts: list[dict]) -> list[str]`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_knowledge_module.py` の名前を新しい偽物に合わせる。

```bash
sed -i '' -e 's/reading-1/knowledge-1/g' -e 's/hub\.readings/hub.knowledge/g' \
  -e 's/has_reading_db/has_knowledge_db/g' -e 's/add_reading/add_article/g' \
  -e 's/test_without_a_reading_db_/test_without_knowledge_/' tests/test_knowledge_module.py
```

`test_notion_trash_failure_keeps_the_saved_state_for_retry` の後ろに足す。

```python
async def test_saving_reuses_a_row_dot_made_and_unsaving_trashes_the_row_with_the_same_url(env):
    """Dot が先に同じ URL を Knowledge に入れていたら、2行目を作らずその行を使う。
    控えに保存先が無くても、解除は同じ URL の行をゴミ箱に入れる。"""
    scheduler, assistant, slack, _ = env
    add_post(assistant, "C40", "40.1", "2026-09-26", READING[0])
    dot_row = assistant.hub.add_article({"title": "LLM の話", "url": "https://zenn.dev/x"})
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/x"})
    assert result["saved"] is True and result["errors"] == [] and len(assistant.hub.knowledge) == 1
    assert knowledge(assistant).core.records.get("post", "C40:40.1")["page"] == dot_row

    add_post(assistant, "C40", "40.2", "2026-09-26", READING[1])
    dot_row_y = assistant.hub.add_article({"title": "RAG の話", "url": "https://zenn.dev/y"})
    result = await assistant.module_head_action("save_reading", {"url": "https://zenn.dev/y", "saved": False})
    assert result == {"url": "https://zenn.dev/y", "saved": False, "liked": False, "errors": []}
    assert assistant.hub.trashed == [dot_row_y]
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_knowledge_module.py -v`
Expected: FAIL（`AttributeError: 'FakeHub' object has no attribute 'add_article'` など）

- [ ] **Step 3: 偽物を書き換える**

`src/kei_agent/testing/fakes.py` の `FakeHub` の `has_reading_db = True` を `has_knowledge_db = True` に、`__init__` の `self.readings: dict[str, dict] = {}` を次にする。

```python
        # 知識ホームの Knowledge（ページ ID → {"type", "url"（Article だけ）, "item", "link"（学びだけ）}）
        self.knowledge: dict[str, dict] = {}
```

`add_reading` を次に置き換える。

```python
    def find_article(self, url):
        """同じ URL の Article（ゴミ箱に入れたものは数えない）。"""
        return next((page_id for page_id, row in self.knowledge.items()
                     if row["type"] == "Article" and row["url"] == url.strip() and page_id not in self.trashed), None)

    def add_article(self, item):
        """Knowledge に Article で入れる。同じ URL の行があればそれを返す。"""
        url = str(item.get("url") or "").strip()
        found = self.find_article(url)
        if found:
            return found
        page_id = f"knowledge-{len(self.knowledge) + 1}"
        self.knowledge[page_id] = {"type": "Article", "url": url, "item": item}
        return page_id
```

- [ ] **Step 4: モジュールを書き換える**

`modules/knowledge/module.py` の `head_action` の `errors = await (self._save(posts) if saved else self._forget(posts))` を次にする。

```python
            errors = await (self._save(posts) if saved else self._forget(url, posts))
```

`_save` の `if page is None:` の塊を次にする。

```python
        if page is None:
            if hub is None or not hub.has_knowledge_db:
                errors.append("知識ホームに「Knowledge」がないので、Notion に保存していません")
            else:
                try:
                    page = await self.core.to_thread(hub.add_article, posts[0]["item"])
                    if not page:
                        errors.append("Notion の「Knowledge」の保存先を確認できませんでした")
                except NotionError as e:
                    errors.append(f"Notion の「Knowledge」に保存できませんでした: {e}")
```

`_forget` を次にする。

```python
    async def _forget(self, url: str, posts: list[dict]) -> list[str]:
        """保存を解除して、Knowledge の同じ URL の行をゴミ箱に入れる。控えに保存先が無ければ URL で探す。
        失敗した行は控えを残して再試行できるようにする。"""
        pages = {post["page"] for post in posts if post.get("page")}
        errors = []
        failed = set()
        hub = self.core.hub
        if not pages and hub is not None and hub.has_knowledge_db:
            try:
                found = await self.core.to_thread(hub.find_article, url)
            except NotionError as e:
                return [f"Notion の「Knowledge」で同じ URL の行を探せませんでした: {e}"]
            pages = {found} if found else set()
        for page in pages:
            if hub is None:
                failed.add(page)
                errors.append("Notion につながっていないので、読みものの保存を解除できません")
                continue
            try:
                await self.core.to_thread(hub.trash_page, page)
            except NotionError as e:
                failed.add(page)
                errors.append(f"Notion の「Knowledge」の保存を解除できませんでした: {e}")
        for post in posts:
            if post.get("page") not in failed:
                self._remember(post, None, None)
        return errors
```

- [ ] **Step 5: MCP の道具の説明を直す**

`src/kei_agent/operations/hands_server.py` の `save_reading` の説明を次にする。

```python
    @mcp.tool(description="Mac が以前配信した読みものを URL で指定して、知識ホームの Knowledge に Article として保存する互換窓口"
                          "（同じ URL の行があれば作らない）。saved=false は同じ URL の行をゴミ箱に入れる。"
                          "URL は reading で取得したものを使い、題名で推測しない。クライアントが独自に見つけた記事は、そのクライアントの保存機能を使う。"
                          "返すのは url・saved・liked・errors。errors があれば保存成功として扱わない")
```

`reading` の説明の2行目を次にする。

```python
                          "why（選んだ理由）・liked（依頼者が 👍 した）・saved（知識ホームの Knowledge に入れた）")
```

- [ ] **Step 6: 通ることを確かめる**

Run: `uv run python -m pytest tests/test_knowledge_module.py tests/test_hands.py -v && uvx ruff check modules/knowledge src/kei_agent/testing src/kei_agent/operations tests/test_knowledge_module.py`
Expected: PASS、ruff の指摘なし

- [ ] **Step 7: 全体を確かめる**

Run: `uv run python -m pytest`
Expected: PASS

- [ ] **Step 8: コミット**

```bash
git add modules/knowledge/module.py src/kei_agent/testing/fakes.py src/kei_agent/operations/hands_server.py tests/test_knowledge_module.py
git commit -m "feat: save legacy readings to Knowledge as articles"
```

---

### Task 5: 振り返りの学びを Knowledge に入れる（daily モジュール）

**Files:**
- Modify: `modules/daily/module.py:9`（docstring）、`:96`、`:117`、`:121-144`（`_keep`）
- Modify: `modules/daily/texts.py:84-109`（`RETRO_QUESTION`・`talk_prompt`）
- Modify: `modules/daily/module.toml:27`（コメント）
- Modify: `src/kei_agent/testing/fakes.py:664-672`（`FakeHub` の学び）
- Test: `tests/test_daily_module.py:201-232`

**Interfaces:**
- Consumes: `HubStore.has_knowledge_db`・`add_learning(item, link="") -> tuple[str, str]`・`trash_page`・`append_review_conclusion`（Task 2）、`FakeHub.knowledge`（Task 4）
- Produces:
  - `FakeHub.add_learning(item: dict, link: str = "") -> tuple[str, str]`（`knowledge` に `{"type": "Learning", "item": item, "link": link}`。種類の写し方は本物の `HubStore` だけが持つ）
  - 消すもの: `FakeHub.has_learning_db`、`FakeHub.learnings`
  - 学びの JSON の形: `{"title", "kind": "学び|助言|気づき", "source", "scene", "lesson", "next"}`（`field` は無い）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_daily_module.py` の `LEARNED` から分野を消す。

```python
LEARNED = ('<<kei-agent-final>>\nレビューは結論から書く、を残すね。\n📒 学び\n'
           '[{"title": "レビューは結論から書く", "kind": "助言", "source": "上司との1on1", '
           '"scene": "設計レビュー", "lesson": "先に結論を言うと議論が速い", "next": "次の資料で1行目に結論"}]\n'
           '<<kei-agent-final-end>>')


def learnings(hub) -> list[tuple[str, dict]]:
    """Knowledge に入った学び（記事を除く）。"""
    return [(page_id, row) for page_id, row in hub.knowledge.items() if row["type"] != "Article"]
```

`test_the_review_thread_turns_what_was_learned_into_notes` の docstring を `"""振り返りのスレッドでは、AI が聞き返して言語化し、まとまったら確かめずに Knowledge に残す。"""` にし、222 行目以降を次にする。

```python
    (page_id, kept), = learnings(assistant.hub)
    assert kept["item"]["kind"] == "助言" and "field" not in kept["item"]
    shown = slack.texts()[-1]
    assert "📒 Knowledge に残したよ" in shown and "レビューは結論から書く" in shown and "[{" not in shown
    (row, summary), = assistant.hub.appended                                      # 日別記録にも題とリンク
    assert row == note.id and "レビューは結論から書く" in summary

    # 「直して」で、前に残したものは捨てて差し替える
    claude.behaviors = [{"text": LEARNED.replace("1行目に結論", "冒頭で結論")}]
    await _talk(assistant, "次にどう使うかを直して", "1001.7")
    assert assistant.hub.trashed == [page_id] and len(learnings(assistant.hub)) == 2
```

その後ろに足す。

```python
async def test_review_talk_without_knowledge_says_where_to_fix_and_keeps_the_day_row(env):
    """Knowledge が使えなくても日別記録は残り、知識ホームの設定を確かめるよう知らせる。"""
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": REVIEW_REPLY}]
    await daily(assistant).review("2026-09-18")
    assistant.hub.has_knowledge_db = False
    claude.behaviors = [{"text": LEARNED}]
    await _talk(assistant, "設計レビューのとき", "1001.6")
    notices = "\n".join(kw["text"] for _, kw in slack.calls if kw.get("channel") == "C9")
    assert "agents.csv の knowledge の行の notion" in notices
    assert learnings(assistant.hub) == [] and assistant.hub.notes


def test_the_talk_prompt_asks_for_kinds_but_not_fields():
    prompt = texts.talk_prompt("2026-09-18", "（履歴）", [])
    assert '"kind": "学び|助言|気づき"' in prompt and '"field"' not in prompt
    assert "Knowledge に残すね" in texts.RETRO_QUESTION
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_daily_module.py -v`
Expected: FAIL（`learnings` が空で unpack に失敗、`"field"` がプロンプトにある、など）

- [ ] **Step 3: 偽物を書き換える**

`src/kei_agent/testing/fakes.py` の `has_learning_db = True` と `add_learning` を次に置き換える。

```python
    def add_learning(self, item, link=""):
        """Knowledge に学びを入れる（種類の写し方は本物の HubStore が持つ）。"""
        page_id = f"knowledge-{len(self.knowledge) + 1}"
        self.knowledge[page_id] = {"type": "Learning", "item": item, "link": link}
        return page_id, f"https://notion.example/{page_id}"
```

- [ ] **Step 4: モジュールを書き換える**

`modules/daily/module.py` の docstring 9 行目を `  言語化し、まとまったら確かめずに知識ホームの「Knowledge」に1件1ページで残し、日別記録のレトプラにも題とリンクを足す` に、96 行目の docstring を `"""振り返りのスレッドの返事（学びの会話）。聞き返すか、まとまったら Knowledge に残す。"""` に、117 行目を次にする。

```python
        done = "📒 Knowledge に残したよ。直したいときは、ここに書いてね。\n" + "\n".join(lines) if saved else ""
```

`_keep` を次にする。

```python
    async def _keep(self, req: Request, retro: dict, items: list[dict]) -> list[dict]:
        """学びを Knowledge に入れ、日別記録のレトプラに題とリンクを足す。前に残したもの（直したとき）は捨てる。"""
        hub = self.core.hub
        if hub is None or not hub.has_knowledge_db:
            await self.core.notify_trouble("学びを Knowledge に残せませんでした。知識ホーム（agents.csv の knowledge の行の "
                                           "notion）と kei-agent-hub-setup --apply を確認してください")
            return []
        link = await self.core.permalink(req.channel, req.thread_ts)
        saved = []
        try:
            for old in retro.get("saved") or []:
                await self.core.to_thread(hub.trash_page, old["id"])
            for item in items:
                page_id, url = await self.core.to_thread(hub.add_learning, item, link)
                saved.append({"id": page_id, "url": url, "title": str(item["title"]).strip()})
            if retro.get("note"):
                summary = "\n".join(f"- [{item['title']}]({item['url']})" if item["url"] else f"- {item['title']}"
                                    for item in saved)
                await self.core.to_thread(hub.append_review_conclusion, retro["note"], f"Knowledge に残した学び\n{summary}",
                                          datetime.now(), req.message_ts)
        except NotionError as e:
            await self.core.notify_trouble(f"学びを Knowledge に残せませんでした: {e}")
        self.core.records.update(RETRO, req.thread_ts, saved=saved)
        return saved
```

`modules/daily/texts.py` の `RETRO_QUESTION` を次にする。

```python
RETRO_QUESTION = ("今日、職場や学校で学んだこと・印象に残った助言はある？ ひと言でいいよ。"
                  "少し聞き返して整理したら、Knowledge に残すね。")
```

`talk_prompt` の JSON の形の行を次にする。

```python
        '[{"title": "学びを1行で", "kind": "学び|助言|気づき", '
        '"source": "誰から・どの場面で", "scene": "場面", "lesson": "学んだこと", "next": "次にどう使うか"}]\n'
```

`modules/daily/module.toml` の 27 行目を `# 振り返りのスレッドでの会話（学んだこと・助言を聞き返して言語化し、知識ホームの Knowledge に残す）` にする。

- [ ] **Step 5: 通ることを確かめる**

Run: `uv run python -m pytest tests/test_daily_module.py -v && uvx ruff check modules/daily src/kei_agent/testing tests/test_daily_module.py`
Expected: PASS、ruff の指摘なし

- [ ] **Step 6: 全体を確かめ、古い名前が残っていないことを見る**

Run: `uv run python -m pytest && uvx ruff check . && git grep -n "has_reading_db\|has_learning_db\|add_reading\|READING_PROPERTIES\|LEARNING_PROPERTIES\|reading_ds_id\|learning_ds_id" -- src modules tests`
Expected: すべて PASS、ruff の指摘なし、`git grep` は何も出さない

- [ ] **Step 7: コミット**

```bash
git add modules/daily src/kei_agent/testing/fakes.py tests/test_daily_module.py
git commit -m "feat: keep review learnings in Knowledge"
```

---

### Task 6: 文書

**Files:**
- Modify: `docs/agents/knowledge-agent.md`（全体）
- Modify: `docs/architecture.md`（`### 共通ホーム` の表と、`notion` 列の箇条。origin/main の 180・202-204 行目）
- Modify: `docs/using.md`（origin/main の 46・49 行目）
- Modify: `docs/dots.md`（origin/main の 98 行目）

**Interfaces:**
- Consumes: Task 1〜5 の動き（知識ホームの ID の置き場所、`kei-agent-hub-setup --apply` が作るもの、Knowledge の列）
- Produces: `docs/agents/knowledge-agent.md#知識ホーム`（ほかの文書からのリンク先）

- [ ] **Step 1: 知識の文書を書き換える**

`docs/agents/knowledge-agent.md` を次の全文にする。

````markdown
# 知識（`knowledge`）

Knowledge は Dot がクラウドで受け持つ役割。Mac の AI・A2A プロセスには依頼しない。

| 処理 | 接続先と動作 |
|---|---|
| 興味・知識の確認 | Notion の知識ホーム（「収集」ページと「Knowledge」）を読む |
| 記事・論文の検索 | Web で Zenn・Qiita・arXiv などを探し、元の資料を読む |
| 質問・要約 | Dot が資料を読んで、出典を添えて答える |
| 保存・更新 | 知識ホームの「Knowledge」に直接書く。記事は Source（URL）で照合し、重複を作らない |
| 毎朝の読みもの | Dot の予定から `#4-knowledge` に1記事1親投稿。3記事なら3投稿 |
| 先行研究の新着 | Dot の予定から研究ホームの「先行研究」と `#0-overview` へ |

Mac が閉じていても処理できる。`run(workspace="knowledge")` は使わない。接続できないプラグインがあれば未確認と伝える。

## 知識ホーム

共通ホームと同じくワークスペースの直下のページ。中に置くのは「収集」と「Knowledge」の2つだけ。共通ホームの「リンク」から開ける。

- ページは Notion の画面で作り、Kei Agent の接続に共有する。ページ ID は `agents.csv` の knowledge の行の `notion` 列に書く
- 「収集」と「Knowledge」を作るのは `uv run kei-agent-hub-setup --apply`。同じ名前のものがあれば作らずに使い、Knowledge の表の列を下の順にそろえる。共通ホームに「収集」が残っていれば、移すまで止まる

### Knowledge

| 列 | 種類 | 中身 |
|---|---|---|
| Title | タイトル | 記事の題、または学びを1行で |
| Type | 選択 | Article / Learning / Advice / Insight |
| Summary | 文 | 記事なら要約2文、学びなら学んだことを1〜2文 |
| Source | 文 | 記事なら URL（照合キー。同じ URL の行があれば作らない）、学びなら誰から・どこで |
| Status | 選択 | Unread / Read。Article にだけ付け、学びは空 |

- 学びは振り返りのスレッドの会話から入る（学び→Learning、助言→Advice、気づき→Insight）。本文は「場面・学んだこと・次にどう使うか」と、最後に学びが出た Slack のスレッドのリンク
- 日付は持たない。Notion のページの作成日時を使う

## 定期実行と保存

[プロンプト一覧](../prompts/README.md) の読みもの・先行研究の指示を使う。毎朝の記事選びは、収集の興味と、最近の Article の Title と Summary から好みを読み取る。記事の各スレッドで「詳しく」「要約して」「保存して」と頼める。「保存して」は Knowledge に Title・Type=Article・Summary・Source=URL・Status=Unread で1行入れる。

「収集」ページは、見出し「興味」「情報源」の下に箇条書きで書く。

```markdown
## 興味
- LLM エージェント: agent、tool use、MCP
## 情報源
- Zenn: llm、python
- Qiita: 機械学習
- arXiv: cs.AI
```

## 旧配信記事の保存

`modules/knowledge/` は旧記事の保存処理だけを保持する。MCP `reading` で取得できる旧記事は `save_reading` で Knowledge に Article（Status=Unread）として保存でき、保存に失敗したものも再試行できる。解除は同じ URL の行をゴミ箱に入れる。

新しい記事の検索・要約・配信はこのモジュールを通さない。ローカルの `reading`・`literature` 定期実行と AI プロセスはない。
````

- [ ] **Step 2: 仕組みの文書を直す**

`docs/architecture.md` の「届くホームは `agents.csv` の `notion` 列（共通ホームは overview の行）」を「届くホームは `agents.csv` の `notion` 列（共通ホームは overview の行、知識ホームは knowledge の行）」にする。`### 共通ホーム` の表から「読みもの」「学びのノート」「収集」の3行を消し、代わりに次の1行を足す。

```markdown
| リンク | 知識ホーム（収集と Knowledge。[knowledge-agent.md](agents/knowledge-agent.md#知識ホーム)）へのリンク |
```

表の下の「作るのは `uv run kei-agent-hub-setup --apply`」を「作るのは `uv run kei-agent-hub-setup --apply`（知識ホームの収集と Knowledge も）」にする。

- [ ] **Step 3: 使い方と Dot の文書を直す**

`docs/using.md` の2行を次にする。

```markdown
| 朝の読みものに「保存して」 | 知識ホームの「Knowledge」に Article で入る（同じ URL なら2行にしない） |
```

```markdown
| 振り返りのスレッドで、今日学んだこと・助言 | Dot が聞き返して言語化し、知識ホームの「Knowledge」に Learning・Advice・Insight のどれかで残す |
```

`docs/dots.md` の「学びの会話を「学びのノート」に残して、題とリンクを日別記録にも足す」を「学びの会話を知識ホームの「Knowledge」に残して、題とリンクを日別記録にも足す」にする。

- [ ] **Step 4: 古い名前が残っていないことを確かめる**

Run: `git grep -n "学びのノート\|「読みもの」\|共通ホームの「収集」\|共通ホーム・知識 DB" -- docs ':!docs/superpowers' ':!docs/prompts'`
Expected: 何も出ない

Run: `uv run python -m pytest && uvx ruff check .`
Expected: すべて PASS、ruff の指摘なし

- [ ] **Step 5: コミット**

```bash
git add docs/agents/knowledge-agent.md docs/architecture.md docs/using.md docs/dots.md
git commit -m "docs: describe the knowledge home"
```

---

### Task 7: Dot のプロンプト

**Files:**
- Modify: `docs/prompts/dot-reading.md`（本文の2行）
- Modify: `docs/prompts/dot-review.md`（出力先の行）
- Modify: `docs/prompts/dot-custom-instructions.md`（`#4-knowledge` の行、「保存して」の行、振り返りの学びの行。origin/main の 29・76・81 行目）

**Interfaces:**
- Consumes: Knowledge の列と値（Global Constraints）
- Produces: プロンプトの中の `collection://{{KNOWLEDGE_DS}}`（Task 8 の Step 8 で本物の ID にする）

**前提:** 共通の決まり（` ```text ` の頭の、口調・日時・Slack・Notion・作業記録の箇条）は変えない。`#4-knowledge`（C0C5HBTGJE4）と収集のページの URL も変えない（ページを移しても URL は変わらない）。

- [ ] **Step 1: 読みものの本文を書き換える**

`docs/prompts/dot-reading.md` の「共通ホームの「収集」ページ https://app.notion.com/p/3e74fb5d2d078104b907cd4070213f67 の興味と情報源を読む。…」の行を次にする。

```text
知識ホームの「収集」ページ https://app.notion.com/p/3e74fb5d2d078104b907cd4070213f67 の興味と情報源を読む。知識ホームの「Knowledge」（collection://{{KNOWLEDGE_DS}}）で Type が Article の最近の行の Title と Summary から好みを読み取り、近いものを優先して、直近24時間の記事から3〜5件選ぶ。公開日と本文を確かめ、読めなかった記事を読んだことにしない。
```

「この処理では Notion に書かない（作業記録は除く）。「読みもの」に保存するのは、利用者が選んだ記事だけ。」の行を次にする。

```text
この処理では Notion に書かない（作業記録は除く）。「Knowledge」に保存するのは、利用者が選んだ記事だけ。
```

- [ ] **Step 2: 振り返りの出力先を書き換える**

`docs/prompts/dot-review.md` の出力先の行を次にする（学びの返事を Knowledge に入れるのは継続指示側。本文は変えない）。

```markdown
- 出力先: Slack `#0-overview`、共通ホームの「日別記録」、知識ホームの「Knowledge」（学びの返事は継続指示で入れる）
```

- [ ] **Step 3: 継続指示を書き換える**

`docs/prompts/dot-custom-instructions.md` の3行を次にする。

```text
- #4-knowledge: 知識ホームの「収集」と「Knowledge」（collection://{{KNOWLEDGE_DS}}）を Notion で読み、Zenn・Qiita・arXiv などを Web で検索して答える。保存・更新も Notion に直接行う。knowledge を run に渡さない
```

```text
- 読みものの投稿に「保存して」「よかった」: 旧ローカル配信分で MCP reading にある記事だけは save_reading(url, saved=true) を使う。saved と errors を見て保存成功を伝える。Dot 自身が選んだ記事は Notion プラグインで知識ホームの「Knowledge」に、Title・Type=Article・Summary（要約2文）・Source=URL・Status=Unread で1行作る（Source が同じ行があれば作らない）
```

```text
- 振り返りのスレッドでの、学んだこと・助言の返事: 聞き返して言語化し、知識ホームの「Knowledge」に1件1ページで残す。Title（学びを1行で）・Type（学び→Learning、助言→Advice、気づき→Insight）・Summary（学んだことを1〜2文）・Source（誰から・どこで）を書き、Status は空のまま。本文に「場面」「学んだこと」「次にどう使うか」の見出しと中身を書き、最後にそのスレッドの Slack のリンクを書く
```

「ローカルの読みものに「保存を解除して」」の行はそのまま（`save_reading(saved=false)` が同じ URL の行をゴミ箱に入れる）。

- [ ] **Step 4: 確かめる**

Run: `git grep -n "「読みもの」\|学びのノート\|共通ホームの「収集」\|共通ホームの知識\|339b3a19" -- docs/prompts`
Expected: 何も出ない

Run: `git diff -U0 docs/prompts | grep '^[-+]' | grep -c "口調\|Dot Work Log\|日時は日本時間"`
Expected: `0`（共通の決まりは変えていない）

Run: `uv run python -m pytest tests/test_docs_contract.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add docs/prompts
git commit -m "docs: point Dot prompts at the Knowledge database"
```

---

### Task 8: Notion の移し替えと切り替え（本物に触る。壊す・書き換える段の前に利用者に確認）

**Files:** なし（Notion・Dot・`~/.config/kei-agent/agents.csv` の操作）。Step 8 だけ `docs/prompts/` を書き換えてコミットする

**前提:** Task 1〜7 の PR がレビュー済みで、マージできる状態。研究と授業のプランは main に入っている。spec の移し方の順（作る → 写す → 確かめる → 切り替える → 消す）で進める（決めたこと 3）。

- [ ] **Step 1: 今の行の数を控える（読むだけ）**

Notion の MCP（`notion-fetch` で view の全ページ）で、共通ホームの「読みもの」と「学びのノート」の行を全部読み、行の数と題の一覧を控える。利用者に数を見せる。

- [ ] **Step 2: 知識ホームを作る（利用者の確認のあと）**

「ワークスペースの直下に「知識ホーム」のページを作ります。よいですか」と聞く。よければ、利用者に Notion の画面で作ってもらう（決めたこと 2）。続けて、利用者に次をしてもらう。

1. 知識ホームを Kei Agent の接続に共有する（ページの「…」→ 接続）
2. `~/.config/kei-agent/agents.csv` の knowledge の行の `notion` 列に、知識ホームの URL の末尾32文字を書く（本物の設定なので、利用者が書く）

Run: `uv run kei-agent doctor`
Expected: 「agents.csv の knowledge の行の notion が書いてある」が出る（この時点の main のコードでも、notion 列はもう読める）

- [ ] **Step 3: 収集を移し、共通ホームにリンクを置く（利用者の確認のあと）**

「共通ホームの「収集」（https://app.notion.com/p/3e74fb5d2d078104b907cd4070213f67）を知識ホームへ移し、共通ホームの「リンク」に知識ホームへのリンクを足します。よいですか」と聞く。よければ Notion の MCP の `notion-move-pages` で収集を知識ホームの下へ移し、`notion-update-page` で共通ホームの「リンク」に知識ホームへのリンクを足す。移したあとも収集の URL が同じで、中身（興味・情報源）が変わっていないことを `notion-fetch` で確かめる。

- [ ] **Step 4: Knowledge を作る（利用者の確認のあと）**

「知識ホームに Knowledge の DB を作ります（列は Title・Type・Summary・Source・Status の順）。よいですか」と聞く。よければ Notion の MCP の `notion-create-database` で、親を知識ホームにして次の列を、この順に作る。

- Title（title）
- Type（select: Article / Learning / Advice / Insight）
- Summary（rich_text）
- Source（rich_text）
- Status（select: Unread / Read）

表のビューの列がこの順に並んでいることを `notion-fetch` で確かめる（違っていれば Step 7 の setup がそろえる）。Knowledge の data source の ID（`collection://…`）を控える。

- [ ] **Step 5: 行を写す（利用者の確認のあと）**

写す内容を利用者に見せて、よいか聞く。よければ Notion の MCP で、Knowledge に次のとおり1行ずつ作る。

- 読みもの → Type=Article: 名前→Title、要約→Summary、URL→Source、状態（気になる→Unread、読んだ→Read）→Status。出どころ・興味・日付は捨てる。Source が同じ行がもうあれば作らない。URL が空の行は、どうするか利用者に聞く
- 学びのノート → 名前→Title、種類（学び→Learning、助言→Advice、気づき→Insight）→Type、本文の「学んだこと」の中身→Summary、出典→Source、Status は空。本文はそのまま写し、最後に Slack の列のリンクを「Slack のスレッド」のリンクとして足す（Slack が空なら足さない）。分野・日付は捨てる

写した行の作成日時は今日になる（Notion の API は作成日時を書けない）。元の日付が分からなくなることを、写す前に利用者に伝えておく。

- [ ] **Step 6: 数と中身を照らし合わせる**

Knowledge の行を全部読み、次を表にして利用者に見せる。

- Type=Article の行の数と、読みものの行の数（Step 1）
- Type が Learning・Advice・Insight の行の数と、学びのノートの行の数（Step 1）
- 古い DB にあって Knowledge に無い題を1件ずつ（0件になるまで直す）。Step 1 のあとに古い DB に増えた行（その間も古いコードと Dot が書く）があれば、それも写す

- [ ] **Step 7: コードを切り替える（利用者の確認のあと）**

利用者に PR をマージしてもらい、`deploy/README.md` のやり方で Kei Agent を起動し直してもらう（launchd は main を動かす）。続けて次を動かす。

Run: `uv run kei-agent-hub-setup`（`--apply` なし）
Expected: 「知識ホーム: <ID>」「Knowledge: <Step 4 の DB の ID>」が出て、止まらない（収集は Step 3 で移してある）。利用者に見せて、適用してよいか聞く

Run: `uv run kei-agent-hub-setup --apply`
Expected: 「適用完了: …、Knowledge <Step 4 の data source の ID>」。`注意:` が出たら利用者に見せる。`~/.local/state/kei-agent/hub.json` の `knowledge_ds_id` が Step 4 の ID で、`reading_*`・`learning_*` の鍵が無いことを確かめる

Run: `uv run kei-agent doctor`
Expected: Notion の行に ERROR が無い

- [ ] **Step 8: Dot のプロンプトに ID を書き入れる**

```bash
git switch main && git pull --ff-only && git switch -c docs/knowledge-prompt-ids
sed -i '' 's/{{KNOWLEDGE_DS}}/<Step 4 で控えた data source の ID>/g' docs/prompts/dot-reading.md docs/prompts/dot-custom-instructions.md
git grep -n "{{KNOWLEDGE_DS}}" docs/prompts
```

Expected: `git grep` は何も出さない。`<Step 4 で控えた data source の ID>` は、Step 4 で控えた `collection://` の後ろの文字列そのものに置き換えてから実行する

Run: `uv run python -m pytest tests/test_docs_contract.py -v`
Expected: PASS

```bash
git add docs/prompts
git commit -m "docs: fill the Knowledge data source id in Dot prompts"
```

利用者に PR を出してマージしてよいか聞く。

- [ ] **Step 9: Dot を切り替える（利用者の確認のあと）**

読みもの・振り返りの定期実行の指示と、継続指示（`dot-custom-instructions.md`）の全文を、今日と同じやり方で Dot に送ってよいか聞く。よければ「要約・言い換え・追記をせず、全文をそのまま保存して」と添えて送り、保存後の照合の返事を確かめる。`save_reading`・`reading` の説明を変えたので、ChatGPT の MCP の接続を作り直してもらう（利用者の操作）。

- [ ] **Step 10: 動きを確かめる**

spec の確かめ方を、利用者と一緒に見る。

- 記事のスレッドで「保存して」と言うと、Knowledge に Article で1行入り、もう一度言っても2行にならない
- 振り返りの学びの返事が、Knowledge に Learning・Advice・Insight のどれかで入り、本文の最後に Slack のリンクがある
- 翌朝の読みものが、知識ホームの収集を読んで選ばれる（Dot Work Log と Slack で確かめる）

- [ ] **Step 11: 古い DB を消す（利用者の確認のあと）**

Step 6 の表をもう一度見せ、「共通ホームの「読みもの」と「学びのノート」の DB をゴミ箱に入れます（30日間は戻せます）。よいですか」と聞く。はっきり「よい」と返ってから、Notion の画面か MCP でゴミ箱に入れる。1つずつ入れ、それぞれ入ったことを確かめる。ゴミ箱を空にはしない。

Run: `uv run kei-agent-hub-setup`（`--apply` なし）
Expected: 止まらず、読みもの・学びのノートの行が出ない
