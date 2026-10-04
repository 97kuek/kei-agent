"""成績のファイルから作った学業記録を、授業ホームの成績・単位要件の DB に書き込む。

ファイルの読み方と、学校ごとの対応（成績の学期と授業 DB の Term、単位要件の名前）は学校の部品
（school.py）が持つ。ここは、どの学校でも同じ書き込み方だけを受け持つ。
行は照合キー（Record ID）で1度だけ作り、2回目からは差分だけを直す。成績に当たる授業が無ければ、
終わった授業（Status＝Done）として1度だけ作って結ぶ。科目区分・科目群は成績にだけ書く。GPA は保存しない。
各 DB は1回の実行で1度だけ読み、照合は手元の索引で行う。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from kei_agent_a2a.api import Notion, NotionError, gateway_notion, load_config

from .academic_record import KIND_OTHER, KINDS, AcademicRecord, Grade, Requirement
from .course_identity import normalize_course_name
from .notion_props import number, plain, select, text, title
from .notion_setup import DONE
from .notion_sync import read_state
from .school import School, from_config

_ACADEMIC_KEYS = ("grades", "requirements")
RECORD_ID = "Record ID"


@dataclass(frozen=True)
class AcademicImportResult:
    created: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]
    ambiguous_relations: tuple[str, ...] = ()
    # 成績に当たる授業が無かったので作った授業（科目名 / 年度 / Term）
    new_courses: tuple[str, ...] = ()


def grade_key(grade: Grade) -> str:
    return f"grade:{grade.year}:{grade.term}:{grade.course_name}"


def requirement_key(requirement: Requirement) -> str:
    return f"requirement:{requirement.group}:{requirement.name}"


def _unique(kind: str, keys: list[str]) -> None:
    """同じ識別子が2つあれば、書き込む前に止める（後の行で前の行を黙って上書きしない）。"""
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise ValueError(f"{kind} の識別子が重複しています: {'、'.join(duplicates)}")


def _course_key(name: str, year: object, term: str, school: School) -> tuple[str, object, str]:
    return normalize_course_name(name), year, school.course_term(term)


def _split_category(category: str) -> tuple[str, str]:
    """「科目群 / 科目区分」を (Group, Category) に分ける。区分が無ければ Category は空。"""
    group, _, sub = category.partition(" / ")
    return group, sub


class AcademicSync:
    """派生済み AcademicRecord を Record ID で一度だけ記録する。"""

    def __init__(self, notion, state: dict, school: School):
        databases = state["databases"]
        self.notion = notion
        self.school = school
        self.sources = {key: databases[key]["data_source_id"] for key in ("courses", *_ACADEMIC_KEYS)}

    def _rows(self, key: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{self.sources[key]}/query", {"page_size": 100})

    def _index(self, key: str) -> dict[str, dict]:
        """Record ID → 既存の行。Notion 側で重複していたら、どちらを直すか決められないので止める。"""
        rows = [(plain(row.get("properties", {}).get(RECORD_ID)), row) for row in self._rows(key)]
        rows = [(identity, row) for identity, row in rows if identity]
        _unique(f"Notion の {key}", [identity for identity, _row in rows])
        return dict(rows)

    def _course_index(self) -> dict[tuple, list[str]]:
        """（科目名, 年度, Term）→ 授業のページ ID。"""
        index: dict[tuple, list[str]] = {}
        for row in self._rows("courses"):
            props = row.get("properties", {})
            key = _course_key(plain(props.get("Name")), number(props.get("Year")), select(props.get("Term")),
                              self.school)
            index.setdefault(key, []).append(row["id"])
        return index

    def _new_course(self, grade: Grade) -> str:
        """成績に当たる授業が無いとき、終わった授業として作る（科目区分・科目群は書かない）。"""
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self.sources["courses"]},
            "properties": {
                "Name": title(grade.course_name), "Status": {"select": {"name": DONE}},
                "Year": {"number": grade.year}, "Term": {"select": {"name": self.school.course_term(grade.term)}},
                "Credits": {"number": grade.credits},
            },
        })
        return page["id"]

    def _upsert(self, key: str, index: dict[str, dict], identity: str,
                properties: dict) -> tuple[str, tuple[str, ...]]:
        row = index.get(identity)
        if row is None:
            self.notion.request("POST", "/pages", {"parent": {"type": "data_source_id",
                                                              "data_source_id": self.sources[key]},
                                                   "properties": properties})
            return "created", ()
        existing = row.get("properties", {})
        write_properties = dict(properties)
        # 手で結び直した relation は上書きしない
        preserved = tuple(
            name for name, wanted in properties.items()
            if "relation" in wanted
            and (current := (existing.get(name) or {}).get("relation") or [])
            and tuple(item.get("id") for item in current)
            != tuple(item.get("id") for item in wanted["relation"])
        )
        for name in preserved:
            write_properties.pop(name)
        if self._properties_match(existing, write_properties):
            return "unchanged", preserved
        self.notion.request("PATCH", f"/pages/{row['id']}", {"properties": write_properties})
        return "updated", preserved

    @staticmethod
    def _properties_match(existing: dict, wanted: dict) -> bool:
        """Notion の応答と write payload のうち、管理する値だけを比較する。"""
        def value(prop: dict) -> object:
            if "title" in prop or "rich_text" in prop:
                return plain(prop)
            if "number" in prop:
                return prop.get("number")
            if "select" in prop:
                return select(prop)
            if "relation" in prop:
                return tuple(item.get("id") for item in prop.get("relation") or [])
            return prop

        return all(value(existing.get(name, {})) == value(expected) for name, expected in wanted.items())

    def sync(self, record: AcademicRecord) -> AcademicImportResult:
        _unique("成績", [grade_key(grade) for grade in record.grades])
        _unique("単位要件", [requirement_key(item) for item in record.requirements])
        indexes = {key: self._index(key) for key in _ACADEMIC_KEYS}
        courses = self._course_index()
        counts = {outcome: dict.fromkeys(_ACADEMIC_KEYS, 0) for outcome in ("created", "updated", "unchanged")}
        ambiguous: list[str] = []
        new_courses: list[str] = []
        for grade in record.grades:
            identity = grade_key(grade)
            group, category = _split_category(grade.category)
            properties = {
                "Name": title(grade.course_name), "Grade": text(grade.grade), "GP": {"number": grade.gp},
                "Credits": {"number": grade.credits}, "Category": text(category), "Group": text(group),
                RECORD_ID: text(identity),
            }
            label = f"成績: {grade.course_name} / {grade.year} / {grade.term}"
            key = _course_key(grade.course_name, grade.year, grade.term, self.school)
            matches = courses.get(key, [])
            existing = indexes["grades"].get(identity) or {}
            linked = ((existing.get("properties") or {}).get("Course") or {}).get("relation")
            if len(matches) > 1:
                ambiguous.append(label)
            elif not matches and not linked:
                matches = courses[key] = [self._new_course(grade)]
                new_courses.append(f"{grade.course_name} / {grade.year} / {self.school.course_term(grade.term)}")
            if len(matches) == 1:
                properties["Course"] = {"relation": [{"id": matches[0]}]}
            outcome, preserved = self._upsert("grades", indexes["grades"], identity, properties)
            if preserved:
                ambiguous.append(f"{label}（既存の{'・'.join(preserved)} relation を保持）")
            counts[outcome]["grades"] += 1
        for requirement in record.requirements:
            identity = requirement_key(requirement)
            kind = requirement.kind if requirement.kind in KINDS else KIND_OTHER
            outcome, _ = self._upsert("requirements", indexes["requirements"], identity, {
                "Name": title(requirement.name), "Remaining": {"number": requirement.remaining},
                "Required": {"number": requirement.required}, "Counted": {"number": requirement.included},
                "Group": text(requirement.group), "Kind": {"select": {"name": kind}}, RECORD_ID: text(identity),
            })
            counts[outcome]["requirements"] += 1
        return AcademicImportResult(counts["created"], counts["updated"], counts["unchanged"],
                                    tuple(ambiguous), tuple(new_courses))


def requirement_changes(rows: dict[str, list[dict]], school: School) -> dict[str, dict]:
    """成績の Group・Category から単位要件が1つに決まるものだけを結ぶ。手で結んだつながりは残す。"""
    requirements: dict[tuple[str, str], list[str]] = {}
    for page in rows["requirements"]:
        props = page["properties"]
        requirements.setdefault((plain(props.get("Group")), plain(props.get("Name"))), []).append(page["id"])
    changes: dict[str, dict] = {}
    for page in rows["grades"]:
        props = page["properties"]
        group, category = plain(props.get("Group")), plain(props.get("Category"))
        if not group or not category or (props.get("Requirement") or {}).get("relation"):
            continue
        # 要件側の名前が成績側と違うものは、学校の部品が候補を足す
        target: list[str] = []
        for name in school.requirement_names(group, category):
            target = requirements.get((group, name), [])
            if target:
                break
        if len(target) == 1:
            changes[page["id"]] = {"Requirement": {"relation": [{"id": target[0]}]}}
    return changes


def link_requirements(notion: Notion, state: dict, school: School) -> int:
    """成績の Requirement に単位要件を結ぶ（単位要件の Grades は Notion が戻り側として持つ）。結んだ数を返す。"""
    rows = {key: notion.paginate("POST", f"/data_sources/{state['databases'][key]['data_source_id']}/query",
                                 {"page_size": 100})
            for key in _ACADEMIC_KEYS}
    changes = requirement_changes(rows, school)
    for page_id, properties in changes.items():
        notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
    return len(changes)


def main(argv: list[str] | None = None) -> int:
    """成績のファイルを学校の部品で読んで dry-run し、明示時だけ Notion へ書き込む入口。"""
    parser = argparse.ArgumentParser(
        prog="kei-agent-module course academic-import",
        description="成績のファイルを学校の部品（config.toml の [course] school）で読み、授業ホームに入れる")
    parser.add_argument("files", type=Path, nargs="+",
                        help="成績のファイル（早稲田は、成績の HTML と単位の HTML をこの順に2つ）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-inputs", action="store_true")
    args = parser.parse_args(argv)
    try:
        school = from_config(load_config())
        record = school.read_record(args.files)
    except (ValueError, OSError) as e:
        raise SystemExit(f"成績を読めません: {e}") from None
    print(f"成績 {len(record.grades)} 件・単位要件 {len(record.requirements)} 件")
    if args.dry_run:
        print("dry-run: Notion への書き込みは行いません")
        return 0
    if not args.apply:
        print("Notion へ反映するには --apply を付けてください")
        return 2
    try:
        notion = gateway_notion("course")
    except NotionError as e:
        raise SystemExit(str(e)) from None
    state = read_state()
    result = AcademicSync(notion, state, school).sync(record)
    links = link_requirements(notion, state, school)
    print("作成 " + "・".join(f"{key} {count} 件" for key, count in result.created.items()))
    print("更新 " + "・".join(f"{key} {count} 件" for key, count in result.updated.items()))
    print(f"単位要件とのつながり {links} 件")
    if result.new_courses:
        print("「授業」に終わった授業として足した: " + "、".join(result.new_courses))
    if result.ambiguous_relations:
        print("手で確認が必要な relation: " + "、".join(result.ambiguous_relations))
    total = sum(result.created.values()) + sum(result.updated.values()) + sum(result.unchanged.values())
    expected = len(record.grades) + len(record.requirements)
    if total != expected:
        raise RuntimeError(f"反映件数の検証に失敗しました: expected={expected}, actual={total}")
    if args.delete_inputs:
        for path in args.files:
            path.unlink()
        print("入力のファイルを削除しました")
    return 0
