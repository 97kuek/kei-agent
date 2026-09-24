"""授業ホームの学業記録 DB を安全に点検する。"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from kei_agent.notion import Notion
from kei_agent_course.academic_record import AcademicRecord, GPAEntry, Grade, Requirement, parse_academic_record
from kei_agent_course.notion_setup import SPECS
from kei_agent_course.notion_sync import TOKEN_ENV, read_state


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
                existing = row.get("properties", {})
                if self._properties_match(existing, properties):
                    return "unchanged"
                self.notion.request("PATCH", f"/pages/{row['id']}", {"properties": properties})
                return "updated"
        self.notion.request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": self.sources[key]},
                                                   "properties": properties})
        return "created"

    @classmethod
    def _properties_match(cls, existing: dict, wanted: dict) -> bool:
        """Notion の応答と write payload のうち、管理する値だけを比較する。"""
        def value(prop: dict) -> object:
            if "title" in prop or "rich_text" in prop:
                return cls._plain(prop)
            if "number" in prop:
                return prop.get("number")
            if "select" in prop:
                return (prop.get("select") or {}).get("name")
            if "relation" in prop:
                return tuple(item.get("id") for item in prop.get("relation") or [])
            return prop

        return all(value(existing.get(name, {})) == value(expected) for name, expected in wanted.items())

    def _course_matches(self, grade: Grade) -> list[str]:
        term = {"春期": "春学期", "秋期": "秋学期"}.get(grade.term, grade.term)
        return [row["id"] for row in self._rows("courses") if (
            self._plain(row.get("properties", {}).get("科目名", {})) == grade.course_name
            and (row.get("properties", {}).get("年度") or {}).get("number") == grade.year
            and ((row.get("properties", {}).get("学期") or {}).get("select") or {}).get("name") == term
        )]

    def sync(self, record: AcademicRecord) -> AcademicImportResult:
        created = {"grades": 0, "requirements": 0, "gpa": 0}
        updated = {"grades": 0, "requirements": 0, "gpa": 0}
        unchanged = {"grades": 0, "requirements": 0, "gpa": 0}
        ambiguous: list[str] = []
        for grade in record.grades:
            identity = grade_key(grade)
            properties = {
                "科目名": {"title": [{"text": {"content": grade.course_name}}]}, "Kei Agent 成績ID": _text(identity),
                "取得年度": {"number": grade.year}, "学期": {"select": {"name": grade.term}},
                "単位": {"number": grade.credits}, "成績": _text(grade.grade),
                "GP": {"number": grade.gp}, "科目区分": _text(grade.category),
            }
            matches = self._course_matches(grade)
            if len(matches) == 1:
                properties["授業"] = {"relation": [{"id": matches[0]}]}
            elif len(matches) > 1:
                ambiguous.append(f"成績履歴: {grade.course_name} / {grade.year} / {grade.term}")
            outcome = self._upsert("grades", "Kei Agent 成績ID", identity, properties)
            ({"created": created, "updated": updated, "unchanged": unchanged}[outcome])["grades"] += 1
        for requirement in record.requirements:
            identity = requirement_key(requirement)
            outcome = self._upsert("requirements", "Kei Agent 要件ID", identity, {
                "要件名": {"title": [{"text": {"content": requirement.name}}]}, "Kei Agent 要件ID": _text(identity),
                "大区分": _text(requirement.group), "所定単位": {"number": requirement.required},
                "既得単位": {"number": requirement.earned}, "算入単位": {"number": requirement.included},
                "残り単位": {"number": requirement.remaining}, "集計種別": {"select": {"name": requirement.kind}},
            })
            ({"created": created, "updated": updated, "unchanged": unchanged}[outcome])["requirements"] += 1
        for entry in record.gpa:
            identity = gpa_key(entry)
            outcome = self._upsert("gpa", "Kei Agent GPAID", identity, {
                "期間": {"title": [{"text": {"content": entry.period}}]}, "Kei Agent GPAID": _text(identity),
                "年度": {"number": entry.year}, "種別": {"select": {"name": entry.kind}}, "GPA": {"number": entry.gpa},
            })
            ({"created": created, "updated": updated, "unchanged": unchanged}[outcome])["gpa"] += 1
        return AcademicImportResult(created, updated, unchanged, tuple(ambiguous))


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


def main(argv: list[str] | None = None) -> int:
    """成績 HTML の解析を dry-run し、明示時だけ Notion へ書き込む入口。"""
    parser = argparse.ArgumentParser(prog="kei-agent-course-academic-import")
    parser.add_argument("grades_html", type=Path)
    parser.add_argument("credits_html", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-inputs", action="store_true")
    args = parser.parse_args(argv)
    record = parse_academic_record(args.grades_html, args.credits_html)
    print(f"成績 {len(record.grades)} 件・単位要件 {len(record.requirements)} 件・GPA {len(record.gpa)} 件")
    if args.dry_run:
        print("dry-run: Notion への書き込みは行いません")
        return 0
    if not args.apply:
        print("Notion へ反映するには --apply を付けてください")
        return 2
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} が設定されていません")
    state = read_state()
    result = AcademicSync(Notion(token), state).sync(record)
    print("作成 " + "・".join(f"{key} {count} 件" for key, count in result.created.items()))
    print("更新 " + "・".join(f"{key} {count} 件" for key, count in result.updated.items()))
    if result.ambiguous_relations:
        print("手で確認が必要な relation: " + "、".join(result.ambiguous_relations))
    total = sum(result.created.values()) + sum(result.updated.values()) + sum(result.unchanged.values())
    expected = len(record.grades) + len(record.requirements) + len(record.gpa)
    if total != expected:
        raise RuntimeError(f"反映件数の検証に失敗しました: expected={expected}, actual={total}")
    if args.delete_inputs:
        args.grades_html.unlink()
        args.credits_html.unlink()
        print("入力 HTML を削除しました")
    return 0
