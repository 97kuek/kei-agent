# 授業ホーム Notion DB 統一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 授業ホーム配下の 6 DB を一つの state と relation graph で管理し、学業記録を安全に upsert できるようにする。

**Architecture:** `CourseSetup` を schema/relation の唯一の宣言場所にし、`notion-course.json` に全 data source を記録する。新しい academic importer は HTML parser の派生 `AcademicRecord` だけを受け、stable source key で DB 行を upsert し、入力から一意に分かる relation だけを設定する。既存の重複・曖昧な対応は mutation せず migration report に載せる。

**Tech Stack:** Python 3.12、Notion REST API client、dataclasses、pytest、ruff。

**Spec:** `docs/superpowers/specs/2026-09-23-execution-boundary-cleanup-design.md`

## Global Constraints

- 正本は授業ホーム配下の `授業`、`課題`、`学習ログ`、`📊 成績履歴`、`🎓 単位要件`、`📈 GPA推移` の 6 DB。
- `授業` は現在・過去の科目台帳。予定と時間カードは `履修中` かつ当該学期のみを読む。
- 成績 HTML の原文は Notion に保存しない。登録件数と relation 検証が成功するまでローカル入力を消さない。
- source が示さない要件→成績や GPA→成績の対応を推測しない。
- 実在する DB/page の移動、アーカイブ、削除、統合は dry-run report と依頼者確認なしにしない。
- コミット・push は依頼者の明示指示があるまで行わない。

## Review Focus

- 初回 setup と二回目 setup が同じ 6 DB を使い、重複 DB を増やさないことを Task 1 で確認する。
- `成績履歴` の relation が過去年次の `授業` に向き、現在予定から漏れることを Task 2 で確認する。
- 同じ HTML を二回 import してもページ数を増やさないことを Task 3 で確認する。
- 同名科目が複数の年度・学期にある入力を Task 3 で ambiguity として report し、勝手に relation を張らないことを確認する。
- DB 名の衝突・不足 property・未管理 child DB を Task 2 の dry-run report で可視化する。

---

## File Structure

- Modify: `src/kei_agent_course/notion_setup.py` — 6 DB schema、relation、state reconciliation。
- Create: `src/kei_agent_course/academic_sync.py` — AcademicRecord の upsert と migration report。
- Modify: `src/kei_agent_course/notion_sync.py` — shared state validation と `CourseNotion` の course lookup を共通化。
- Modify: `src/kei_agent_course/academic_record.py` — stable source key に必要な正規化 helper のみを追加。
- Modify: `pyproject.toml` — `kei-agent-course-academic-import` entry point。
- Modify: `tests/test_course_ics.py`, `tests/test_course_sync.py`, `tests/test_academic_record.py`; Create: `tests/test_academic_sync.py`。
- Modify: `plugin/course/skills/managing-academic-record/SKILL.md`, `plugin/course/skills/managing-course-notion/SKILL.md`, `docs/notion-layout.md`, `docs/agents/course.md`。

### Task 1: 6 DB schema と relation graph を setup に定義する

**Files:**
- Modify: `src/kei_agent_course/notion_setup.py`
- Modify: `tests/test_course_ics.py`

**Interfaces:**
- Produces: `SPECS: dict[str, tuple[str, dict]]` with keys `courses`, `assignments`, `study_logs`, `grades`, `requirements`, `gpa`。
- Produces: `CourseSetup.run(courses: list[tuple[str, str, int | None]] | None = None) -> None` that persists all six DB IDs/properties.

- [ ] **Step 1: six DB と relation の failing test を書く**

```python
def test_course_setup_creates_six_canonical_databases_and_relations(tmp_path):
    setup = notion_setup.CourseSetup(FakeNotion(), "home", tmp_path / "notion-course.json")
    setup.run()
    assert set(setup.state["databases"]) == {
        "courses", "assignments", "study_logs", "grades", "requirements", "gpa",
    }
    assert relation_target(setup, "assignments", "科目") == setup.state["databases"]["courses"]["data_source_id"]
    assert relation_target(setup, "study_logs", "科目") == setup.state["databases"]["courses"]["data_source_id"]
    assert relation_target(setup, "grades", "科目") == setup.state["databases"]["courses"]["data_source_id"]
    assert relation_target(setup, "requirements", "算入成績") == setup.state["databases"]["grades"]["data_source_id"]
    assert relation_target(setup, "gpa", "対象成績") == setup.state["databases"]["grades"]["data_source_id"]
```

- [ ] **Step 2: failure を確認する**

Run: `uv run --group dev --group agents pytest tests/test_course_ics.py::test_course_setup_creates_six_canonical_databases_and_relations -q`

Expected: grade/requirement/gpa keys がないため FAIL。

- [ ] **Step 3: canonical schema を実装する**

```python
GRADES = {
    "properties": {
        "タイトル": {"title": {}}, "Kei Agent 成績ID": {"rich_text": {}},
        "取得年度": {"number": {"format": "number"}}, "学期": {"select": {"options": []}},
        "単位": {"number": {"format": "number"}}, "成績": {"select": {"options": []}},
        "GP": {"number": {"format": "number"}}, "科目区分": {"rich_text": {}},
    },
    "relations": {"科目": ("courses", "成績履歴"),
                  "単位要件": ("requirements", "算入成績"),
                  "GPA推移": ("gpa", "対象成績")},
}
```

`REQUIREMENTS` に `Kei Agent 要件ID`、`名称`、`区分`、`所定`、`既得`、`算入`、`残り` と `算入成績` relation を、`GPA` に `Kei Agent GPAID`、`期間`、`年度`、`種別`、`GPA` と `対象成績` relation を定義する。`SPECS` の canonical title は既存の `授業`、`課題`、`学習ログ` と仕様の絵文字付き academic DB を使う。

- [ ] **Step 4: idempotent setup regression を追加する**

```python
def test_course_setup_second_run_does_not_create_more_databases(tmp_path):
    notion = FakeNotion(persist_children=True)
    setup = notion_setup.CourseSetup(notion, "home", tmp_path / "state.json")
    setup.run(); created = notion.created_database_count
    setup.run()
    assert notion.created_database_count == created == 6
```

- [ ] **Step 5: setup test suite を通す**

Run: `uv run --group dev --group agents pytest tests/test_course_ics.py -q`

Expected: PASS。

### Task 2: state validation と dry-run migration report を追加する

**Files:**
- Create: `src/kei_agent_course/academic_sync.py`
- Modify: `src/kei_agent_course/notion_sync.py`
- Modify: `tests/test_course_sync.py`
- Create: `tests/test_academic_sync.py`

**Interfaces:**
- Produces: `REQUIRED_DATABASES = frozenset({"courses", "assignments", "study_logs", "grades", "requirements", "gpa"})`。
- Produces: `MigrationReport(missing: tuple[str, ...], duplicates: tuple[str, ...], unmanaged: tuple[str, ...], ambiguous_courses: tuple[str, ...])`。
- Produces: `inspect_course_home(notion: Notion, home_page_id: str, state: dict) -> MigrationReport` without mutations.

- [ ] **Step 1: state/report failing tests を書く**

```python
def test_read_state_rejects_legacy_three_database_state(tmp_path):
    state = {"databases": {key: {} for key in ("courses", "assignments", "study_logs")}}
    (tmp_path / "notion-course.json").write_text(json.dumps(state))
    with pytest.raises(SyncError, match="kei-agent-course-setup"):
        read_state(tmp_path / "notion-course.json")

def test_inspect_reports_duplicates_without_archiving_them():
    report = inspect_course_home(FakeNotion(children=["授業", "授業", "雑記"]), "home", {"databases": {}})
    assert report.duplicates == ("授業",)
    assert report.unmanaged == ("雑記",)
    assert FakeNotion.write_calls == []
```

- [ ] **Step 2: failure を確認する**

Run: `uv run --group dev --group agents pytest tests/test_course_sync.py tests/test_academic_sync.py -q`

Expected: legacy state が通る、report API がないため FAIL。

- [ ] **Step 3: shared state validator と non-mutating inspect を実装する**

```python
def require_course_databases(state: dict) -> dict[str, dict]:
    databases = state.get("databases") or {}
    if not REQUIRED_DATABASES <= databases.keys():
        raise SyncError(NO_STATE)
    return databases

def inspect_course_home(notion, home_page_id, state):
    children = notion.children(home_page_id)
    titles = [child["child_database"]["title"] for child in children if child["type"] == "child_database"]
    counts = Counter(titles)
    canonical = {title for title, _ in SPECS.values()}
    return MigrationReport(
        missing=tuple(sorted(canonical - set(titles))),
        duplicates=tuple(sorted(title for title, count in counts.items() if title in canonical and count > 1)),
        unmanaged=tuple(sorted(title for title in titles if title not in canonical)),
        ambiguous_courses=(),
    )
```

`CourseNotion` と import command の両方が `require_course_databases()` を使う。report は標準出力/return だけで Notion API の write endpoint を呼ばない。

- [ ] **Step 4: historical course lookup test を追加する**

```python
def test_current_courses_hides_finished_course_but_grade_lookup_keeps_it(notion):
    notion.rows["courses"] = [active_course("統計"), finished_course("数学", 2025, "春学期")]
    client = CourseNotion(notion, full_state())
    assert [row["subject"] for row in client.current_courses()] == ["統計"]
    assert client.match_course("数学", 2025, "春期") == "math-page"
```

- [ ] **Step 5: state/report suite を通す**

Run: `uv run --group dev --group agents pytest tests/test_course_sync.py tests/test_academic_sync.py -q`

Expected: PASS。

### Task 3: academic record を stable key で upsert する

**Files:**
- Modify: `src/kei_agent_course/academic_record.py`
- Modify: `src/kei_agent_course/academic_sync.py`
- Modify: `tests/test_academic_record.py`
- Modify: `tests/test_academic_sync.py`

**Interfaces:**
- Produces: `grade_key(grade: Grade) -> str`, `requirement_key(requirement: Requirement) -> str`, `gpa_key(entry: GPAEntry) -> str`。
- Produces: `AcademicSync.sync(record: AcademicRecord) -> AcademicImportResult` with `created`, `updated`, `unchanged`, `ambiguous_relations`.

- [ ] **Step 1: idempotence and ambiguity failing tests を書く**

```python
def test_academic_sync_upserts_same_record_without_duplicate_pages(notion):
    sync = AcademicSync(notion, full_state())
    first = sync.sync(sample_record())
    second = sync.sync(sample_record())
    assert first.created == {"grades": 1, "requirements": 2, "gpa": 3}
    assert second.created == {"grades": 0, "requirements": 0, "gpa": 0}

def test_ambiguous_same_name_course_is_reported_without_relation(notion):
    notion.rows["courses"] = [finished_course("数学", 2025, "春学期"), finished_course("数学", 2026, "春学期")]
    result = AcademicSync(notion, full_state()).sync(record_with_grade("数学", 2024, "春期"))
    assert result.ambiguous_relations == ("成績履歴: 数学 / 2024 / 春期",)
    assert relation_ids(notion.rows["grades"][0], "科目") == []
```

- [ ] **Step 2: failure を確認する**

Run: `uv run --group dev --group agents pytest tests/test_academic_sync.py tests/test_academic_record.py -q`

Expected: importer/key API がないため FAIL。

- [ ] **Step 3: deterministic keys と upsert を実装する**

```python
def grade_key(grade):
    return f"grade:{grade.year}:{grade.term}:{grade.course_name}:{grade.credits:g}:{grade.grade}"

def upsert(self, data_source_id, key_property, key, properties):
    row = self.rows_by_key(data_source_id, key_property).get(key)
    if row is None:
        self.notion.request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties})
        return "created"
    if changed(row, properties):
        self.notion.request("PATCH", f"/pages/{row['id']}", {"properties": properties})
        return "updated"
    return "unchanged"
```

Grades match a course only on `(course_name, year, normalized_term)` exactly once. Requirements and GPA rows never receive guessed grade relations; their relation properties remain empty until an authoritative mapping exists. Validate created/updated/unchanged counts before CLI removes any input file.

- [ ] **Step 4: source preservation/error test を足す**

```python
def test_import_failure_does_not_delete_input_html(tmp_path, monkeypatch):
    grades, credits = write_valid_html_pair(tmp_path)
    monkeypatch.setattr(AcademicSync, "sync", lambda *_: (_ for _ in ()).throw(SyncError("Notion failure")))
    assert main([str(grades), str(credits)]) == 1
    assert grades.exists() and credits.exists()
```

- [ ] **Step 5: academic sync suite を通す**

Run: `uv run --group dev --group agents pytest tests/test_academic_record.py tests/test_academic_sync.py -q`

Expected: PASS。

### Task 4: CLI、skill、documentation を safe migration workflow に合わせる

**Files:**
- Modify: `src/kei_agent_course/academic_sync.py`
- Modify: `pyproject.toml`
- Modify: `plugin/course/skills/managing-academic-record/SKILL.md`
- Modify: `plugin/course/skills/managing-course-notion/SKILL.md`
- Modify: `docs/notion-layout.md`, `docs/agents/course.md`
- Modify: `tests/test_plugins.py`

- [ ] **Step 1: command contract の failing test を書く**

```python
def test_academic_import_cli_supports_dry_run_and_requires_explicit_apply():
    assert main(["--dry-run", "grades.html", "credits.html"]) == 0
    assert FakeNotion.write_calls == []
    assert main(["grades.html", "credits.html"]) == 2  # --apply がない

def test_course_skill_names_all_six_canonical_databases():
    text = Path("plugin/course/skills/managing-academic-record/SKILL.md").read_text()
    for name in ("授業", "課題", "学習ログ", "📊 成績履歴", "🎓 単位要件", "📈 GPA推移"):
        assert name in text
```

- [ ] **Step 2: failure を確認する**

Run: `uv run --group dev --group agents pytest tests/test_academic_sync.py tests/test_plugins.py -q`

Expected: entry point/CLI copy がないため FAIL。

- [ ] **Step 3: explicit CLI を実装する**

```toml
[project.scripts]
kei-agent-course-academic-import = "kei_agent_course.academic_sync:main"
```

`kei-agent-course-academic-import --dry-run <grades.html> <credits.html>` は schema/report/upsert の予定だけを表示して write しない。`--apply` は import 成功後に件数・relation 検証を表示するが、入力 HTML の削除は `--delete-inputs` を明示した場合だけにする。CLI は token を表示しない。

- [ ] **Step 4: docs と skill を更新する**

`docs/notion-layout.md` に 6 DB の図、relation graph、dry-run → `--apply` → 必要なら `--delete-inputs` の順を記す。skill は GPA/単位回答時に 3 academic DB を正本として query し、DB にない値を推測しないこと、曖昧 relation を手で確認することを記す。`managing-course-notion` は schema の削除・改名を import を壊す操作として確認必須にする。

- [ ] **Step 5: full verification を実行する**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run --group dev --group agents pytest tests/test_course_ics.py tests/test_course_sync.py tests/test_academic_record.py tests/test_academic_sync.py tests/test_plugins.py -q && UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uvx ruff check src/kei_agent_course tests && git diff --check`

Expected: すべて成功、diff check は出力なし。実際の Notion へはこの段階で書き込まない。
