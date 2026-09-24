# 授業ホーム Notion DB 統一・表示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 既存の授業ホームのデータを保持したまま、6 DB の relation、課題整理 view、Moodle 課題名、GPA 推移 chart を統一する。

**Architecture:** 既存の Notion プロパティ名を正本とする schema adapter を `notion_setup.py` と `academic_sync.py` に置く。書込みを伴う Notion レイアウト処理は `course_layout.py` に分離し、`--dry-run` を既定にした明示的な移行コマンドからだけ実行する。

**Tech Stack:** Python 3.13、Notion API `2026-03-11`、pytest、ruff、uv。

**Spec:** `docs/superpowers/specs/2026-09-24-course-notion-unification-migration-design.md`

## Global Constraints

- 既存の正本 DB、ページ、プロパティ、relation、入力済みデータは削除しない。
- `Untitled` の2 DBは変更・アーカイブ・移動・削除しない。
- 課題タイトルは厳密に `「…」の提出期限` の形式だけを `…` にする。
- 既存課題ページの本文が空でないとき、ブロックを追加・変更しない。
- GPA の値・成績・要件 relation を推測して書き込まない。
- 外部書込みは `kei-agent-course-layout --apply` と `kei-agent-course-setup` の明示実行だけで行う。

## Review Focus

- `📊 成績履歴` の title が `科目名` の既存 schema で、二つ目の title property を作ろうとせずセットアップが完了すること（Task 1）。
- 同名の正本 DB が二つあるとき、どちらも選ばず停止し、データを書き換えないこと（Task 1）。
- `「…」の提出期限` 以外の Moodle 題名、特にアンケート終了や受験可能期間の題名を変えないこと（Task 2）。
- 手書きの既存本文がある課題ページにテンプレートを重ねないこと（Task 2）。
- view が既に存在するとき、重複作成せず同じ view を更新し、`課題一覧` の4列だけを指定順で可視化すること（Task 3）。

---

## File structure

- `src/kei_agent_course/notion_setup.py` — 既存の学業DBプロパティ名に合わせた schema と relation 補完。
- `src/kei_agent_course/academic_sync.py` — 既存 schema 名で成績・要件・GPAを stable ID により upsert。
- `src/kei_agent_course/notion_sync.py` — Moodle 課題名の正規化、新規課題ページの空本文テンプレート。
- `src/kei_agent_course/course_layout.py` — 課題 title の既存行移行、課題 table view と GPA chart view の dry-run/apply。
- `pyproject.toml` — `kei-agent-course-layout` の CLI エントリ。
- `tests/test_academic_sync.py` — schema adapter と relation 補完の unit test。
- `tests/test_course_sync.py` — 題名正規化と課題ページ本文の unit test。
- `tests/test_course_layout.py` — view payload と dry-run/apply の unit test。
- `docs/agents/course.md`、`docs/notion-layout.md`、`deploy/README.md` — 6 DB、課題整理、layout 移行の運用手順。

### Task 1: 既存学業 DB の schema adapter と relation 補完

**Files:**
- Modify: `src/kei_agent_course/notion_setup.py`
- Modify: `src/kei_agent_course/academic_sync.py`
- Modify: `tests/test_academic_sync.py`

**Interfaces:**
- Consumes: `CourseSetup.run(courses: list[tuple[str, str, int | None]] | None) -> None`
- Produces: `SPECS` が既存DBの `科目名`、`要件名`、`所定単位`、`既得単位`、`算入単位`、`残り単位`、`授業` を正本として扱い、課題DBの既存 title `タイトル` を同じ property ID のまま `課題` に改名する。
- Produces: `AcademicSync.sync(record: AcademicRecord) -> AcademicImportResult` が同じ既存プロパティ名に書き込む。

- [ ] **Step 1: 既存 title 名の失敗テストを書く**

```python
def test_setup_uses_existing_grade_title_instead_of_adding_a_second_title(tmp_path):
    notion = ExistingCourseHomeNotion(grade_title="科目名")
    CourseSetup(notion, "home", tmp_path / "notion-course.json").run()
    assert ("PATCH", "/data_sources/grades", {"properties": {"タイトル": {"title": {}}}}) not in notion.calls
    assert "Kei Agent 成績ID" in notion.data_sources["grades"]["properties"]


def test_setup_renames_existing_assignment_title_without_creating_another_title(tmp_path):
    notion = ExistingCourseHomeNotion(assignment_title="タイトル")
    CourseSetup(notion, "home", tmp_path / "notion-course.json").run()
    assert notion.property_renamed("assignments", "タイトル", "課題")
    assert notion.title_property_count("assignments") == 1
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --group dev --group course pytest -q tests/test_academic_sync.py -k existing_grade_title`

Expected: `Cannot create new title property` 相当の失敗、または `タイトル` を追加しようとした assertion failure。

- [ ] **Step 3: schema を既存プロパティ名へ寄せる**

まず `ASSIGNMENTS` の title property を `課題` と定義し、`TITLE_ALIASES = {"assignments": {"課題": "タイトル"}}` を追加する。`CourseSetup.database()` は canonical property がなければ alias の既存 title property を検出し、同じ property ID をキーに `PATCH /data_sources/{id}` して `課題` へ改名してから不足プロパティを再評価する。新たな title property は作らない。

次に `GRADES`、`REQUIREMENTS`、`GPA` を次のキーにする。`GRADES` の course relation は既存の `授業` を使い、`REQUIREMENTS` と `GPA` にだけ grades への新しい dual relation を追加する。

```python
GRADES["properties"] = {
    "科目名": {"title": {}}, "Kei Agent 成績ID": {"rich_text": {}},
    "取得年度": {"number": {"format": "number"}}, "学期": {"select": {"options": []}},
    "単位": {"number": {"format": "number"}}, "成績": {"rich_text": {}},
    "GP": {"number": {"format": "number"}}, "科目区分": {"rich_text": {}},
}
GRADES["relations"] = {"授業": ("courses", "成績履歴")}
REQUIREMENTS["relations"] = {"算入成績": ("grades", "単位要件")}
GPA["relations"] = {"対象成績": ("grades", "GPA推移")}
```

`AcademicSync.sync()` の payload も `科目名`、`授業`、`要件名`、`所定単位`、`既得単位`、`算入単位`、`残り単位` を使う。既存 relation は追加しない。

- [ ] **Step 4: relation 補完と upsert のテストを足す**

```python
def test_setup_adds_only_missing_grade_relations(tmp_path):
    notion = ExistingCourseHomeNotion()
    CourseSetup(notion, "home", tmp_path / "notion-course.json").run()
    assert {"算入成績", "対象成績"} <= notion.added_relation_names()
    assert notion.deleted == []


def test_academic_sync_writes_existing_property_names():
    result = AcademicSync(existing_schema_notion(), full_state()).sync(one_grade_record())
    grade = result_written_page("grades")
    assert set(grade["properties"]) >= {"科目名", "Kei Agent 成績ID", "授業"}
```

- [ ] **Step 5: テストを通す**

Run: `uv run --group dev --group course pytest -q tests/test_academic_sync.py tests/test_course_sync.py`

Expected: PASS。

- [ ] **Step 6: コミットする**

```bash
git add src/kei_agent_course/notion_setup.py src/kei_agent_course/academic_sync.py tests/test_academic_sync.py
git commit -m "fix: adapt course setup to existing notion schema"
```

### Task 2: Moodle 課題名と空ページの整理枠

**Files:**
- Modify: `src/kei_agent_course/notion_sync.py`
- Modify: `tests/test_course_sync.py`

**Interfaces:**
- Produces: `assignment_title(summary: str) -> str`。
- Produces: `assignment_template_blocks() -> list[dict]`。
- Consumes: `CourseNotion.sync(events: list[Event], known_only: bool = True) -> Result`。

- [ ] **Step 1: 失敗する正規化・テンプレートテストを書く**

```python
def test_assignment_title_removes_only_quoted_submission_suffix():
    assert assignment_title("「Assignment A」の提出期限") == "Assignment A"
    assert assignment_title("【ミニテスト】PC の受験可能期間の終了") == "【ミニテスト】PC の受験可能期間の終了"


def test_new_assignment_gets_sections_but_existing_page_body_is_preserved():
    notion = FakeNotion(courses=COURSE_ROWS, page_children={"new-row": []})
    _sync(notion, [REPORT])
    assert notion.appended_children("new-row") == ["やること", "提出物", "進捗メモ", "資料・リンク"]
    notion.page_children["existing-row"] = [{"type": "paragraph"}]
    notion.ensure_assignment_template("existing-row")
    assert notion.appended_children("existing-row") == []
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --group dev --group course pytest -q tests/test_course_sync.py -k 'assignment_title or assignment_gets_sections'`

Expected: `ImportError` または title が raw summary のままの assertion failure。

- [ ] **Step 3: 最小の正規化と空本文だけのテンプレートを実装する**

```python
_QUOTED_DUE = re.compile(r"^「(?P<title>.+)」の提出期限$")

def assignment_title(summary: str) -> str:
    match = _QUOTED_DUE.fullmatch(summary.strip())
    return match.group("title").strip() if match else summary.strip()

def assignment_template_blocks() -> list[dict]:
    return [
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": title}}]}}
        for title in ("やること", "提出物", "進捗メモ", "資料・リンク")
    ]
```

`_properties()` と `_differs()` は raw `event.summary` ではなく `assignment_title(event.summary)` を使う。新規ページ POST の返却 ID を受け、`GET /blocks/{page_id}/children` が空のときだけ `PATCH /blocks/{page_id}/children` で上の blocks を追加する。

- [ ] **Step 4: テストを通す**

Run: `uv run --group dev --group course pytest -q tests/test_course_sync.py`

Expected: PASS。既存の Moodle 同期の idempotency test も通る。

- [ ] **Step 5: コミットする**

```bash
git add src/kei_agent_course/notion_sync.py tests/test_course_sync.py
git commit -m "feat: organize moodle assignment pages"
```

### Task 3: 課題 table view と GPA line chart の idempotent な生成

**Files:**
- Create: `src/kei_agent_course/course_layout.py`
- Modify: `pyproject.toml`
- Create: `tests/test_course_layout.py`

**Interfaces:**
- Produces: `assignment_view_payload(state: dict) -> dict`。
- Produces: `gpa_view_payload(state: dict) -> dict`。
- Produces: `apply_layout(notion: Notion, state: dict, apply: bool) -> LayoutReport`。
- CLI: `kei-agent-course-layout [--apply]`。既定は dry-run。

- [ ] **Step 1: view payload の失敗テストを書く**

```python
def test_assignment_view_shows_only_the_requested_columns_in_order():
    payload = assignment_view_payload(full_state())
    columns = payload["configuration"]["properties"]
    assert [column["property_id"] for column in columns[:4]] == [
        property_id("assignments", "科目"), property_id("assignments", "課題"),
        property_id("assignments", "締切"), property_id("assignments", "状態"),
    ]
    assert all(column["visible"] for column in columns[:4])
    assert all(not column["visible"] for column in columns[4:])


def test_gpa_view_is_a_line_chart_of_raw_period_and_gpa_values():
    payload = gpa_view_payload(full_state())
    config = payload["configuration"]
    assert config["chart_type"] == "line"
    assert config["x_axis_property_id"] == property_id("gpa", "期間")
    assert config["y_axis_property_id"] == property_id("gpa", "GPA")
    assert config["sort"] == "x_ascending"
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --group dev --group course pytest -q tests/test_course_layout.py -k 'view'`

Expected: `ModuleNotFoundError: kei_agent_course.course_layout`。

- [ ] **Step 3: view payload と upsert を実装する**

Notion の View API に以下の payload を送る。既存 view は `GET /views?database_id=<id>` で名前を探し、あれば `PATCH /views/<id>`、なければ `POST /views` を使う。

```python
def gpa_view_payload(state: dict) -> dict:
    props = state["databases"]["gpa"]["properties"]
    return {
        "name": "GPA推移", "type": "chart",
        "configuration": {
            "type": "chart", "chart_type": "line",
            "x_axis_property_id": props["期間"], "y_axis_property_id": props["GPA"],
            "sort": "x_ascending", "color_theme": "blue", "height": "medium",
            "axis_labels": "both", "grid_lines": "horizontal", "show_data_labels": True,
            "smooth_line": False, "hide_line_fill_area": False,
        },
        "sorts": [{"property": "年度", "direction": "ascending"}],
    }
```

`assignment_view_payload()` は `name: "課題一覧"`、`type: "table"`、締切昇順の `sorts` と、`科目`、`課題`、`締切`、`状態` の visible property list を返す。`apply=False` では HTTP mutation を行わず、作成・更新予定だけを `LayoutReport` に残す。

- [ ] **Step 4: dry-run と view upsert のテストを足す**

```python
def test_dry_run_never_mutates_notion():
    notion = FakeNotion()
    report = apply_layout(notion, full_state(), apply=False)
    assert report.planned == ("課題一覧: create", "GPA推移: create")
    assert notion.writes == []


def test_apply_updates_existing_named_view_without_duplicate():
    notion = FakeNotion(views=[{"id": "assignment-view", "name": "課題一覧"}])
    apply_layout(notion, full_state(), apply=True)
    assert notion.calls_for("PATCH", "/views/assignment-view")
    assert not notion.calls_for("POST", "/views")
```

- [ ] **Step 5: テストを通す**

Run: `uv run --group dev --group course pytest -q tests/test_course_layout.py`

Expected: PASS。

- [ ] **Step 6: コミットする**

```bash
git add src/kei_agent_course/course_layout.py pyproject.toml tests/test_course_layout.py
git commit -m "feat: add course notion layout views"
```

### Task 4: 既存課題の安全な一回限りのレイアウト移行

**Files:**
- Modify: `src/kei_agent_course/course_layout.py`
- Modify: `tests/test_course_layout.py`
- Modify: `docs/agents/course.md`
- Modify: `docs/notion-layout.md`
- Modify: `deploy/README.md`

**Interfaces:**
- Consumes: `apply_layout(notion: Notion, state: dict, apply: bool) -> LayoutReport`。
- Produces: `reconcile_assignment_pages(notion: Notion, state: dict, apply: bool) -> AssignmentLayoutReport`。
- CLI: `kei-agent-course-layout` が dry-run で対象件数を表示し、`--apply` 時だけ page title・空本文・view を変更する。

- [ ] **Step 1: 既存課題ページ移行の失敗テストを書く**

```python
def test_reconcile_updates_only_exact_moodle_titles_and_empty_pages():
    notion = FakeNotion(assignments=[assignment("p1", "「課題#1」の提出期限"), assignment("p2", "アンケート終了")],
                        page_children={"p1": [], "p2": [{"type": "paragraph"}]})
    report = reconcile_assignment_pages(notion, full_state(), apply=True)
    assert report.renamed == 1 and report.templated == 1
    assert notion.page_title("p1") == "課題#1"
    assert notion.page_title("p2") == "アンケート終了"
    assert notion.appended_children("p2") == []
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --group dev --group course pytest -q tests/test_course_layout.py -k reconcile`

Expected: `ImportError` または report の assertion failure。

- [ ] **Step 3: dry-run/apply を実装する**

`課題` data source を paginate し、title の exact normalization と empty page body を読む。`apply=False` は `renamed` と `templated` の予定件数だけを返す。`apply=True` は title PATCH と blocks PATCH を対象行にだけ行う。課題 title property の `タイトル` から `課題` への同一 ID 改名は Task 1 のセットアップだけが担い、この task は既存ページの title 値と本文だけを扱う。

- [ ] **Step 4: docs を更新する**

`docs/agents/course.md`、`docs/notion-layout.md`、`deploy/README.md` に次を追記する。

```zsh
uv run --group course kei-agent-course-layout
uv run --group course kei-agent-course-layout --apply
```

`--apply` 前の dry-run 確認、`Untitled` 非変更、課題本文の非破壊条件、GPA graph のデータ非推測を明記する。

- [ ] **Step 5: 関連テストと静的検査を通す**

Run: `uv run --group dev --group course pytest -q tests/test_academic_sync.py tests/test_course_sync.py tests/test_course_layout.py && uvx ruff check . && git diff --check`

Expected: PASS。

- [ ] **Step 6: コミットする**

```bash
git add src/kei_agent_course/course_layout.py tests/test_course_layout.py docs/agents/course.md docs/notion-layout.md deploy/README.md
git commit -m "docs: document course notion layout migration"
```

### Task 5: 実 Notion の移行、Moodle 照合、最終検証

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: `kei-agent-course-setup <home_page_id>` と `kei-agent-course-layout [--apply]`。
- Produces: state に6 DB が入り、課題一覧 view・GPA推移 view と安全な課題整理が反映される。

- [ ] **Step 1: 実 Notion の dry-run を取る**

Run:

```zsh
source ~/.config/zsh/local/kei-agent-course.zsh
uv run --group course kei-agent-course-setup <授業ホームID>
uv run --group course kei-agent-course-layout
```

Expected: 6 DB の state、追加予定 relation、title 正規化予定件数、空ページへのテンプレート予定件数、view の作成/更新予定を表示する。

- [ ] **Step 2: 実 Notion へ apply する**

Run:

```zsh
source ~/.config/zsh/local/kei-agent-course.zsh
uv run --group course kei-agent-course-setup <授業ホームID>
uv run --group course kei-agent-course-layout --apply
```

Expected: `Untitled` を触らず、正本6 DB の relation、`課題一覧`、`GPA推移`、必要な title・空本文だけを反映する。

- [ ] **Step 3: 読み取り専用で結果を検証する**

Run:

```zsh
source ~/.config/zsh/local/kei-agent-course.zsh
uv run --group course kei-agent-course-inspect
uv run --group dev --group agents pytest -q
uvx ruff check .
```

Expected: Moodle の登録科目・授業DBの照合、全テスト、lint が成功する。

- [ ] **Step 4: コミットを確認して handoff する**

```bash
git log --oneline main..HEAD
git status --short
```

Expected: source と docs のコミットだけがあり、Notion token・state 内容はコミットに含まれない。
