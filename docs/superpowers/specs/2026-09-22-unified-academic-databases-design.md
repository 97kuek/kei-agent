# Unified Academic Databases Design

## Goal

Make `授業` the canonical record for each academic-year and term course offering, and connect assignments, grades, degree requirements, and GPA summaries without recording import-source metadata.

## Database model

`授業` has one page for a course offering in one academic year and term. It owns the course identity and schedule: `科目名`, `年度`, `学期`, `曜日`, `時限`, `単位`, `科目コード`, `科目群`, `必選区分`, `履修年次`, and `状態`.

`課題` retains its existing `科目` relation to `授業`. `成績履歴` adds a `授業` relation; grade, GP, earned credits, and completion stay on the grade record. `単位要件` adds a `対象授業` relation for the courses counted toward each requirement. `GPA推移` adds an optional `対象授業` relation and keeps one row per academic-year/term summary.

Unknown course category, schedule, year, or requirement mappings are intentionally left empty. Historical rows are linked only when the course name, academic year, and term identify one course offering without ambiguity.

## Views

`授業` keeps its timetable view and gains a view exposing curriculum classification. `GPA推移` gains a line chart that groups by `期間`, averages `GPA`, and sorts the existing chronological period labels ascending.

## Migration and safety

Create all properties and relations before linking pages. Populate or link pages only when the source facts are unambiguous. Existing properties are not removed during this migration; import-source fields can be hidden from views and are eligible for a separately confirmed cleanup after the data model is verified. Existing rows remain intact throughout.
