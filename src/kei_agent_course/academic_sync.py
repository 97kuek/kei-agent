"""授業ホームの学業記録 DB を安全に点検する。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from kei_agent_course.academic_record import AcademicRecord, GPAEntry, Grade, Requirement
from kei_agent_course.notion_setup import SPECS


@dataclass(frozen=True)
class MigrationReport:
    """実データを変更せずに返す DB 整理の確認結果。"""

    missing: tuple[str, ...]
    duplicates: tuple[str, ...]
    unmanaged: tuple[str, ...]
    ambiguous_courses: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcademicImportResult:
    created: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]
    ambiguous_relations: tuple[str, ...] = ()


def grade_key(grade: Grade) -> str:
    return f"grade:{grade.year}:{grade.term}:{grade.course_name}:{grade.credits:g}:{grade.grade}"


def requirement_key(requirement: Requirement) -> str:
    return f"requirement:{requirement.group}:{requirement.name}"


def gpa_key(entry: GPAEntry) -> str:
    return f"gpa:{entry.year}:{entry.kind}"


def _text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}]}


class AcademicSync:
    """派生済み AcademicRecord を stable key で一度だけ記録する。"""

    def __init__(self, notion, state: dict):
        databases = state["databases"]
        self.notion = notion
        self.sources = {key: databases[key]["data_source_id"] for key in ("courses", "grades", "requirements", "gpa")}

    def _rows(self, key: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{self.sources[key]}/query", {"page_size": 100})

    @staticmethod
    def _plain(prop: dict) -> str:
        parts = prop.get("rich_text") or prop.get("title") or []
        return "".join(str(part.get("plain_text") or part.get("text", {}).get("content") or "") for part in parts)

    def _upsert(self, key: str, id_property: str, identity: str, properties: dict) -> str:
        for row in self._rows(key):
            if self._plain(row.get("properties", {}).get(id_property, {})) == identity:
                return "unchanged"
        self.notion.request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": self.sources[key]},
                                                   "properties": properties})
        return "created"

    def _course_matches(self, grade: Grade) -> list[str]:
        term = {"春期": "春学期", "秋期": "秋学期"}.get(grade.term, grade.term)
        return [row["id"] for row in self._rows("courses") if (
            self._plain(row.get("properties", {}).get("科目名", {})) == grade.course_name
            and (row.get("properties", {}).get("年度") or {}).get("number") == grade.year
            and ((row.get("properties", {}).get("学期") or {}).get("select") or {}).get("name") == term
        )]

    def sync(self, record: AcademicRecord) -> AcademicImportResult:
        created = {"grades": 0, "requirements": 0, "gpa": 0}
        unchanged = {"grades": 0, "requirements": 0, "gpa": 0}
        ambiguous: list[str] = []
        for grade in record.grades:
            identity = grade_key(grade)
            properties = {
                "タイトル": {"title": [{"text": {"content": grade.course_name}}]}, "Kei Agent 成績ID": _text(identity),
                "取得年度": {"number": grade.year}, "学期": {"select": {"name": grade.term}},
                "単位": {"number": grade.credits}, "成績": {"select": {"name": grade.grade}},
                "GP": {"number": grade.gp}, "科目区分": _text(grade.category),
            }
            matches = self._course_matches(grade)
            if len(matches) == 1:
                properties["科目"] = {"relation": [{"id": matches[0]}]}
            elif len(matches) > 1:
                ambiguous.append(f"成績履歴: {grade.course_name} / {grade.year} / {grade.term}")
            outcome = self._upsert("grades", "Kei Agent 成績ID", identity, properties)
            (created if outcome == "created" else unchanged)["grades"] += 1
        for requirement in record.requirements:
            identity = requirement_key(requirement)
            outcome = self._upsert("requirements", "Kei Agent 要件ID", identity, {
                "名称": {"title": [{"text": {"content": requirement.name}}]}, "Kei Agent 要件ID": _text(identity),
                "区分": _text(requirement.group), "所定": {"number": requirement.required},
                "既得": {"number": requirement.earned}, "算入": {"number": requirement.included}, "残り": {"number": requirement.remaining},
            })
            (created if outcome == "created" else unchanged)["requirements"] += 1
        for entry in record.gpa:
            identity = gpa_key(entry)
            outcome = self._upsert("gpa", "Kei Agent GPAID", identity, {
                "期間": {"title": [{"text": {"content": entry.period}}]}, "Kei Agent GPAID": _text(identity),
                "年度": {"number": entry.year}, "種別": {"select": {"name": entry.kind}}, "GPA": {"number": entry.gpa},
            })
            (created if outcome == "created" else unchanged)["gpa"] += 1
        return AcademicImportResult(created, {key: 0 for key in created}, unchanged, tuple(ambiguous))


def inspect_course_home(notion, home_page_id: str, state: dict) -> MigrationReport:
    """授業ホーム直下の DB を読むだけで点検する。"""
    del state  # state の既存 ID は migration 実行時に使う。dry-run では child DB を正とする。
    titles = [
        str(block.get("child_database", {}).get("title") or "")
        for block in notion.children(home_page_id)
        if block.get("type") == "child_database"
    ]
    canonical = {title for title, _spec in SPECS.values()}
    counts = Counter(titles)
    return MigrationReport(
        missing=tuple(sorted(canonical - set(titles))),
        duplicates=tuple(sorted(title for title, count in counts.items() if title in canonical and count > 1)),
        unmanaged=tuple(sorted(title for title in titles if title and title not in canonical)),
    )
