"""成績 HTML から作った学業記録を、授業ホームの成績・単位要件・GPA の DB に書き込む。

行は stable key（`Kei Agent 成績ID` など）で1度だけ作り、2回目からは差分だけを直す。
各 DB は1回の実行で1度だけ読み、照合は手元の索引で行う。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from kei_agent.notion import Notion, NotionError, gateway_notion
from kei_agent_course import periods
from kei_agent_course.academic_record import AcademicRecord, GPAEntry, Grade, Requirement, parse_academic_record
from kei_agent_course.course_identity import normalize_course_name
from kei_agent_course.notion_props import number, plain, select, text, title
from kei_agent_course.notion_sync import read_state

# 成績の学期 → GPA の期間。夏ク・秋ク・通年は公表値と照合済み。冬クはどちらに入るか確かめていないので結ばない
_GPA_TERMS = {**periods.GRADE_TERMS, "夏ク": periods.SPRING, "秋ク": periods.AUTUMN,
              periods.ALL_YEAR: periods.AUTUMN}
# 科目群の表記（成績 HTML → 授業 DB の選択肢）
_COURSE_GROUPS = {"Ａ群": "A群", "Ｂ群": "B群", "Ｃ群(専門教育科目)": "C群", "他箇所聴講科目": "他箇所聴講科目"}
_ACADEMIC_KEYS = ("grades", "requirements", "gpa")


@dataclass(frozen=True)
class AcademicImportResult:
    created: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]
    ambiguous_relations: tuple[str, ...] = ()


def grade_key(grade: Grade) -> str:
    return f"grade:{grade.year}:{grade.term}:{grade.course_name}"


def requirement_key(requirement: Requirement) -> str:
    return f"requirement:{requirement.group}:{requirement.name}"


def gpa_key(entry: GPAEntry) -> str:
    return f"gpa:{entry.year}:{entry.kind}"


def gpa_display_label(year: int, kind: str) -> str | None:
    """GPA の期間名。グラフで時系列に並ぶよう、年度・学期番号・学期名の順にする。"""
    term = {periods.SPRING: 1, periods.AUTUMN: 2}.get(kind)
    return f"{year} {term} {kind}" if term and year else None


def _unique(kind: str, keys: list[str]) -> None:
    """同じ識別子が2つあれば、書き込む前に止める（後の行で前の行を黙って上書きしない）。"""
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise ValueError(f"{kind} の識別子が重複しています: {'、'.join(duplicates)}")


def _course_key(name: str, year: object, term: str) -> tuple[str, object, str]:
    return normalize_course_name(name), year, periods.course_term(term)


class AcademicSync:
    """派生済み AcademicRecord を stable key で一度だけ記録する。"""

    ID_PROPERTIES = {"grades": "Kei Agent 成績ID", "requirements": "Kei Agent 要件ID", "gpa": "Kei Agent GPAID"}

    def __init__(self, notion, state: dict):
        databases = state["databases"]
        self.notion = notion
        self.sources = {key: databases[key]["data_source_id"] for key in ("courses", *_ACADEMIC_KEYS)}

    def _rows(self, key: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{self.sources[key]}/query", {"page_size": 100})

    def _index(self, key: str) -> dict[str, dict]:
        """識別子 → 既存の行。Notion 側で重複していたら、どちらを直すか決められないので止める。"""
        rows = [(plain(row.get("properties", {}).get(self.ID_PROPERTIES[key])), row) for row in self._rows(key)]
        rows = [(identity, row) for identity, row in rows if identity]
        _unique(f"Notion の {key}", [identity for identity, _row in rows])
        return dict(rows)

    def _course_index(self) -> dict[tuple, list[str]]:
        """（科目名, 年度, 学期）→ 授業のページ ID。"""
        index: dict[tuple, list[str]] = {}
        for row in self._rows("courses"):
            props = row.get("properties", {})
            key = _course_key(plain(props.get("科目名")), number(props.get("年度")), select(props.get("学期")))
            index.setdefault(key, []).append(row["id"])
        return index

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
        _unique("GPA", [gpa_key(entry) for entry in record.gpa])
        indexes = {key: self._index(key) for key in _ACADEMIC_KEYS}
        courses = self._course_index()
        counts = {outcome: dict.fromkeys(_ACADEMIC_KEYS, 0) for outcome in ("created", "updated", "unchanged")}
        ambiguous: list[str] = []
        for grade in record.grades:
            identity = grade_key(grade)
            category_parts = grade.category.split(" / ", 1)
            group = category_parts[0] if len(category_parts) == 2 else grade.category
            subcategory = category_parts[1] if len(category_parts) == 2 else ""
            properties = {
                "授業名": title(grade.course_name), "Kei Agent 成績ID": text(identity),
                "取得年度": {"number": grade.year}, "学期": {"select": {"name": grade.term}},
                "単位": {"number": grade.credits}, "成績": text(grade.grade),
                "GP": {"number": grade.gp}, "科目群": text(group), "科目区分": text(subcategory),
            }
            label = f"成績履歴: {grade.course_name} / {grade.year} / {grade.term}"
            matches = courses.get(_course_key(grade.course_name, grade.year, grade.term), [])
            if len(matches) == 1:
                properties["授業"] = {"relation": [{"id": matches[0]}]}
            elif len(matches) > 1:
                ambiguous.append(label)
            outcome, preserved = self._upsert("grades", indexes["grades"], identity, properties)
            if preserved:
                ambiguous.append(f"{label}（既存の{'・'.join(preserved)} relation を保持）")
            counts[outcome]["grades"] += 1
        for requirement in record.requirements:
            identity = requirement_key(requirement)
            outcome, _ = self._upsert("requirements", indexes["requirements"], identity, {
                "要件名": title(requirement.name), "Kei Agent 要件ID": text(identity),
                "大区分": text(requirement.group), "所定単位": {"number": requirement.required},
                "既得単位": {"number": requirement.earned}, "算入単位": {"number": requirement.included},
                "残り単位": {"number": requirement.remaining}, "集計種別": {"select": {"name": requirement.kind}},
            })
            counts[outcome]["requirements"] += 1
        for entry in record.gpa:
            identity = gpa_key(entry)
            outcome, _ = self._upsert("gpa", indexes["gpa"], identity, {
                "期間": title(gpa_display_label(entry.year, entry.kind) or entry.period),
                "Kei Agent GPAID": text(identity),
                "年度": {"number": entry.year}, "種別": {"select": {"name": entry.kind}}, "GPA": {"number": entry.gpa},
            })
            counts[outcome]["gpa"] += 1
        return AcademicImportResult(counts["created"], counts["updated"], counts["unchanged"], tuple(ambiguous))


def _grade_name(props: dict) -> str:
    return plain(props.get("授業名") or props.get("科目名"))


def academic_relation_changes(rows: dict[str, list[dict]]) -> dict[str, dict]:
    """一意な区分・年度/学期だけを結ぶ。手入力済みの relation は維持する。"""
    requirements: dict[tuple[str, str], list[str]] = {}
    for page in rows["requirements"]:
        props = page["properties"]
        requirements.setdefault((plain(props.get("大区分")), plain(props.get("要件名"))), []).append(page["id"])
    gpas: dict[tuple[int, str], list[str]] = {}
    for page in rows["gpa"]:
        props = page["properties"]
        gpas.setdefault((number(props.get("年度")), select(props.get("種別"))), []).append(page["id"])

    changes: dict[str, dict] = {}
    for page in rows["grades"]:
        props = page["properties"]
        group = plain(props.get("科目群"))
        category = plain(props.get("科目区分"))
        if not group and " / " in category:
            group, category = category.split(" / ", 1)
        patch: dict = {}
        if not props.get("単位要件", {}).get("relation"):
            target = requirements.get((group, category), [])
            # 要件側の名前が成績側と違うもの（学務の表記を確かめて対応づけたものだけ）
            if not target and (group, category) == ("Ｃ群(専門教育科目)", "専門選択必修"):
                target = requirements.get((group, "専門選択必修（学系別専門）"), [])
            if not target and (group, category) in {
                ("Ａ群", "外国語 英語"), ("Ｂ群", "自然科学 物理学"),
                ("Ｂ群", "自然科学 化学"),
            }:
                target = requirements.get((group, category + " 必修"), [])
            if group and category and len(target) == 1:
                patch["単位要件"] = {"relation": [{"id": target[0]}]}
        if not props.get("GPA推移", {}).get("relation"):
            term = select(props.get("学期"))
            kind = _GPA_TERMS.get(term)
            # 成績の「その他」は学期を示さない。既知のαだけは2025春の
            # 公表値との単位加重計算で照合済み。
            if term == "その他" and number(props.get("取得年度")) == 2025 \
                    and _grade_name(props).startswith("データ科学入門α "):
                kind = periods.SPRING
            target = gpas.get((number(props.get("取得年度")), kind), []) if kind else []
            if len(target) == 1:
                patch["GPA推移"] = {"relation": [{"id": target[0]}]}
        if patch:
            changes[page["id"]] = patch
    return changes


def historical_course_changes(grades: list[dict], courses: list[dict]) -> list[dict]:
    """成績の年度・学期・名称から一意な過去授業だけを新規作成する計画。"""
    existing: dict[tuple, list[str]] = {}
    for row in courses:
        props = row["properties"]
        key = _course_key(plain(props.get("科目名")), number(props.get("年度")), select(props.get("学期")))
        if all(key):
            existing.setdefault(key, []).append(row["id"])
    planned: list[dict] = []
    seen: set[tuple] = set()
    for grade in grades:
        props = grade["properties"]
        if (props.get("授業") or {}).get("relation"):
            continue
        name = _grade_name(props)
        year = number(props.get("取得年度"))
        term = periods.course_term(select(props.get("学期")))
        if not name or not year or not term:
            raise ValueError(f"過去授業の識別情報が不足しています: {grade['id']}")
        key = _course_key(name, year, term)
        if key in seen or len(existing.get(key, [])) > 1:
            raise ValueError(f"過去授業の識別子が重複しています: {key}")
        seen.add(key)
        if existing.get(key):
            continue
        group_select = _COURSE_GROUPS.get(plain(props.get("科目群")))
        category = plain(props.get("科目区分"))
        properties = {
            "科目名": title(name), "年度": {"number": year},
            "学期": {"select": {"name": term}}, "単位": {"number": number(props.get("単位"))},
            "状態": {"select": {"name": "終了"}},
            "成績履歴": {"relation": [{"id": grade["id"]}]},
        }
        if group_select:
            properties["科目群"] = {"select": {"name": group_select}}
        if category:
            properties["科目区分"] = {"select": {"name": category}}
        for name_ in ("単位要件", "GPA推移"):
            relation = (props.get(name_) or {}).get("relation") or []
            if relation:
                properties[name_] = {"relation": [{"id": item["id"]} for item in relation]}
        planned.append({"grade_id": grade["id"], "properties": properties})
    return planned


def _missing_options(current: list[dict], wanted: list[dict]) -> list[dict] | None:
    """select の選択肢に足りないものがあれば、既存を残した全体を返す。"""
    names = {item["name"] for item in current}
    additions = [item for item in wanted if item["name"] not in names]
    if not additions:
        return None
    return [{"name": item["name"], "color": item["color"]} for item in current] + additions


def reconcile_academic_history(notion: Notion, state: dict, apply: bool = False) -> dict[str, int]:
    """確定済みの成績から過去授業を作り、検証できる relation を接続する。"""
    from kei_agent_course.notion_setup import COURSES

    sources = {key: state["databases"][key]["data_source_id"] for key in ("courses", *_ACADEMIC_KEYS)}
    rows = {key: notion.paginate("POST", f"/data_sources/{source}/query", {"page_size": 100})
            for key, source in sources.items()}
    relation_patches = academic_relation_changes(rows)
    report = {"requirements": sum("単位要件" in patch for patch in relation_patches.values()),
              "gpa": sum("GPA推移" in patch for patch in relation_patches.values()),
              "historical_courses": len(historical_course_changes(rows["grades"], rows["courses"]))}
    if not apply:
        return report

    source_id = sources["courses"]
    course_props = notion.request("GET", f"/data_sources/{source_id}")["properties"]
    schemas: dict = {}
    for key in ("科目群", "学期"):
        options = _missing_options(course_props[key]["select"]["options"],
                                   COURSES["properties"][key]["select"]["options"])
        if options:
            schemas[key] = {"select": {"options": options}}
    categories = sorted({plain(row["properties"].get("科目区分")) for row in rows["grades"]} - {""})
    existing_categories = (course_props.get("科目区分") or {}).get("select", {}).get("options", [])
    options = _missing_options(existing_categories, [{"name": name, "color": "gray"} for name in categories])
    if "科目区分" not in course_props or options:
        schemas["科目区分"] = {"select": {"options": options or existing_categories}}
    if schemas:
        notion.request("PATCH", f"/data_sources/{source_id}", {"properties": schemas})
    grades = {row["id"]: row for row in rows["grades"]}
    for page_id, properties in relation_patches.items():
        notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
        # 読み直さずに、手元の成績にも同じ relation を反映する（過去授業へ引き継ぐため）
        grades[page_id]["properties"].update(properties)
    for entry in historical_course_changes(rows["grades"], rows["courses"]):
        notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": source_id},
            "properties": entry["properties"],
        })
    return report


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
    try:
        notion = gateway_notion("course")
    except NotionError as e:
        raise SystemExit(str(e)) from None
    state = read_state()
    result = AcademicSync(notion, state).sync(record)
    links = reconcile_academic_history(notion, state, apply=True)
    print("作成 " + "・".join(f"{key} {count} 件" for key, count in result.created.items()))
    print("更新 " + "・".join(f"{key} {count} 件" for key, count in result.updated.items()))
    print("連携 " + "・".join(f"{key} {count} 件" for key, count in links.items()))
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
