# Unified Academic Databases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the academic Notion databases through `授業`, remove import-source metadata, and add a GPA line chart.

**Architecture:** `授業` is the canonical course-offering database. Other databases retain their domain-specific facts and add relations back to the matching `授業` page. Migration only links records with unambiguous matching facts; missing or ambiguous information stays empty.

**Tech Stack:** Notion MCP database schema updates, relations, page updates, and chart views.

**Spec:** `docs/superpowers/specs/2026-09-22-unified-academic-databases-design.md`

## Global Constraints

- Preserve all existing Notion pages and database records.
- Do not fabricate missing timetable, classification, academic-year, or degree-requirement information.
- Do not delete or alter pre-existing properties during this migration.
- Do not expose personal grade values in logs or user-facing messages.

---

### Task 1: Expand the course-of-offering schema

**Files:**

- Modify: Notion data source `collection://5bf4c0f0-42ef-422c-ad71-4267074c46d9` (`授業`)

**Interfaces:**

- Produces: `年度`, `単位`, `科目群`, `必選区分`, and `履修年次` properties on `授業`.

- [ ] **Step 1: Read the existing schema**

Fetch the `授業` database and confirm that `科目名`, `学期`, `曜日`, `時限`, `科目コード`, `状態`, and `課題` remain present.

- [ ] **Step 2: Add canonical course properties**

Run one `update_data_source` request with:

```sql
ADD COLUMN "年度" NUMBER;
ADD COLUMN "単位" NUMBER;
ADD COLUMN "科目群" SELECT('A群':blue, 'B群':green, 'C群':purple, 'その他':gray);
ADD COLUMN "必選区分" SELECT('必修':red, '選択必修':orange, '選択':blue, 'その他':gray);
ADD COLUMN "履修年次" NUMBER;
```

- [ ] **Step 3: Verify the schema**

Fetch `授業` and confirm the five properties have the requested types and that existing course pages are unchanged.

### Task 2: Add cross-database relations

**Files:**

- Modify: Notion data source `collection://e7d6c48a-bb4f-4932-9b2a-9f23d1366d2a` (`成績履歴`)
- Modify: Notion data source `collection://72e8a565-2c7d-4915-9453-6ce61362bfca` (`単位要件`)
- Modify: Notion data source `collection://6f715aba-8ed4-4aa5-b29c-3e1b4466c851` (`GPA推移`)

**Interfaces:**

- Consumes: `授業` data source `collection://5bf4c0f0-42ef-422c-ad71-4267074c46d9`.
- Produces: `授業` relation on `成績履歴`, `対象授業` relation on `単位要件`, and `対象授業` relation on `GPA推移`.

- [ ] **Step 1: Add the grade relation**

Run this update on `成績履歴`:

```sql
ADD COLUMN "授業" RELATION('5bf4c0f0-42ef-422c-ad71-4267074c46d9', DUAL '成績履歴' 'grade_history')
```

- [ ] **Step 2: Add the requirement and GPA relations**

Run these updates on their respective data sources:

```sql
ADD COLUMN "対象授業" RELATION('5bf4c0f0-42ef-422c-ad71-4267074c46d9', DUAL '単位要件' 'degree_requirements')
```

```sql
ADD COLUMN "対象授業" RELATION('5bf4c0f0-42ef-422c-ad71-4267074c46d9', DUAL 'GPA推移' 'gpa_history')
```

- [ ] **Step 3: Verify relation targets**

Fetch all three data sources and confirm each new relation targets `授業`, not `課題` or another database.

### Task 3: Safely migrate unambiguous grade links

**Files:**

- Modify: Notion pages in `成績履歴` and `授業`

**Interfaces:**

- Consumes: `科目名`, `取得年度`, and `学期` from `成績履歴`; `科目名`, `年度`, and `学期` from `授業`.
- Produces: one `授業` relation on each unambiguous grade page; new historical course-offering pages where no matching page exists.

- [ ] **Step 1: Query course and grade rows**

Read only titles, years, and terms. Build a match key from literal fields `(科目名, 年度, 学期)`.

- [ ] **Step 2: Create only missing unambiguous course offerings**

Create a `授業` page for each grade match key that has no existing course page. Set `科目名`, `年度`, `学期`, and the grade row's `単位`. Leave schedule, classification, code, and year-of-study blank.

- [ ] **Step 3: Link the corresponding grade pages**

Set each grade row's `授業` relation to exactly one matching course-offering page. Do not write a relation for match keys that are duplicated or incomplete.

- [ ] **Step 4: Verify record preservation**

Compare the number of grade rows before and after the migration and confirm it is unchanged. Spot-check that every linked grade page points to a course page with the same literal title, year, and term.

### Task 4: Add the GPA chart without deleting existing properties

**Files:**

- Modify: Notion data sources `成績履歴`, `単位要件`, and `GPA推移`
- Modify: Notion database `GPA推移`

**Interfaces:**

- Produces: a `GPA推移` chart view named `GPA推移（折れ線）`; legacy properties remain unchanged.

- [ ] **Step 1: Create the line chart view**

Create a `chart` view on the GPA database named `GPA推移（折れ線）` with:

```text
GROUP BY "期間"; CHART line AGGREGATE average ON "GPA" COLOR blue SORT x_ascending
```

- [ ] **Step 2: Final verification**

Fetch every database and confirm: relations exist, all prior data fields remain, and the GPA line chart is visible.
