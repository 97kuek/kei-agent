"""既存の学業記録を、ページ ID を維持したまま表示・同期用に整える。"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from kei_agent.notion import Notion
from kei_agent_course.course_identity import normalize_course_name
from kei_agent_course.notion_sync import TOKEN_ENV, read_state


def _text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}]}


def _plain(prop: dict) -> str:
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in parts)


def _title(value: str) -> dict:
    return {"title": [{"text": {"content": value}}]}


def _grade_id(props: dict) -> str:
    year = props["取得年度"]["number"]
    term = (props["学期"]["select"] or {})["name"]
    name = _plain(props.get("授業名") or props["科目名"])
    return f"grade:{year}:{term}:{name}"


def gpa_display_label(year: int, kind: str) -> str | None:
    term = {"春学期": 1, "秋学期": 2}.get(kind)
    return f"{year} {term} {kind}" if term and year else None


def academic_page_changes(rows: dict[str, list[dict]]) -> dict[str, dict[str, dict]]:
    """一意な既存行だけに付与する差分を返す。衝突時は書き込み前に停止する。"""
    changes: dict[str, dict[str, dict]] = {key: {} for key in ("grades", "requirements", "gpa")}
    seen: dict[str, set[str]] = {key: set() for key in changes}
    for key, pages in rows.items():
        for page in pages:
            props = page["properties"]
            patch: dict = {}
            if key == "grades":
                identity = _grade_id(props)
                id_name = "Kei Agent 成績ID"
                category = _plain(props.get("科目区分", {}))
                if " / " in category:
                    group, subcategory = category.split(" / ", 1)
                    if not _plain(props.get("科目群", {})):
                        patch["科目群"] = _text(group)
                    patch["科目区分"] = _text(subcategory)
            elif key == "requirements":
                identity = f"requirement:{_plain(props.get('大区分', {}))}:{_plain(props['要件名'])}"
                id_name = "Kei Agent 要件ID"
            elif key == "gpa":
                year = props["年度"]["number"]
                kind = (props["種別"]["select"] or {})["name"]
                identity = f"gpa:{year}:{kind}"
                id_name = "Kei Agent GPAID"
                label = gpa_display_label(year, kind)
                if label and _plain(props["期間"]) != label:
                    patch["期間"] = _title(label)
            else:
                raise ValueError(f"対象外の DB: {key}")
            if identity in seen[key]:
                raise ValueError(f"{key} の識別子が重複しています: {identity}")
            seen[key].add(identity)
            current_id = _plain(props.get(id_name, {}))
            if current_id and current_id != identity:
                raise ValueError(f"{key} の既存識別子が値と一致しません: {page['id']}")
            if not current_id:
                patch[id_name] = _text(identity)
            if patch:
                changes[key][page["id"]] = patch
    return changes


def academic_relation_changes(rows: dict[str, list[dict]]) -> dict[str, dict]:
    """一意な区分・年度/学期だけを結ぶ。手入力済みの relation は維持する。"""
    requirements: dict[tuple[str, str], list[str]] = {}
    for page in rows["requirements"]:
        props = page["properties"]
        key = (_plain(props.get("大区分", {})), _plain(props["要件名"]))
        requirements.setdefault(key, []).append(page["id"])
    gpas: dict[tuple[int, str], list[str]] = {}
    for page in rows["gpa"]:
        props = page["properties"]
        kind = (props.get("種別", {}).get("select") or {}).get("name")
        gpas.setdefault((props.get("年度", {}).get("number"), kind), []).append(page["id"])

    changes: dict[str, dict] = {}
    terms = {"春期": "春学期", "夏ク": "春学期", "秋期": "秋学期",
             "秋ク": "秋学期", "通年": "秋学期"}
    for page in rows["grades"]:
        props = page["properties"]
        group = _plain(props.get("科目群", {}))
        category = _plain(props.get("科目区分", {}))
        if not group and " / " in category:
            group, category = category.split(" / ", 1)
        patch: dict = {}
        if not props.get("単位要件", {}).get("relation"):
            target = requirements.get((group, category), [])
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
            term = (props.get("学期", {}).get("select") or {}).get("name")
            kind = terms.get(term)
            # 成績の「その他」は学期を示さない。既知のαだけは2025春の
            # 公表値との単位加重計算で照合済み。
            if term == "その他" and props.get("取得年度", {}).get("number") == 2025 \
                    and _plain(props.get("授業名") or props.get("科目名", {})).startswith("データ科学入門α "):
                kind = "春学期"
            target = gpas.get((props.get("取得年度", {}).get("number"), kind), []) if kind else []
            if len(target) == 1:
                patch["GPA推移"] = {"relation": [{"id": target[0]}]}
        if patch:
            changes[page["id"]] = patch
    return changes


def historical_course_changes(grades: list[dict], courses: list[dict]) -> list[dict]:
    """成績の年度・学期・名称から一意な過去授業だけを新規作成する計画。"""
    term_names = {"春期": "春学期", "秋期": "秋学期"}
    existing: dict[tuple[int, str, str], list[str]] = {}
    for row in courses:
        props = row["properties"]
        year = (props.get("年度") or {}).get("number")
        term = ((props.get("学期") or {}).get("select") or {}).get("name")
        name = normalize_course_name(_plain(props.get("科目名", {})))
        if year and term and name:
            existing.setdefault((year, term, name), []).append(row["id"])
    planned: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    for grade in grades:
        props = grade["properties"]
        if (props.get("授業") or {}).get("relation"):
            continue
        name = _plain(props.get("授業名") or props.get("科目名", {}))
        year = (props.get("取得年度") or {}).get("number")
        original_term = ((props.get("学期") or {}).get("select") or {}).get("name")
        term = term_names.get(original_term, original_term)
        if not name or not year or not term:
            raise ValueError(f"過去授業の識別情報が不足しています: {grade['id']}")
        key = (year, term, normalize_course_name(name))
        if key in seen or len(existing.get(key, [])) > 1:
            raise ValueError(f"過去授業の識別子が重複しています: {key}")
        seen.add(key)
        if existing.get(key):
            continue
        group = _plain(props.get("科目群", {}))
        group_select = {"Ａ群": "A群", "Ｂ群": "B群", "Ｃ群(専門教育科目)": "C群",
                        "他箇所聴講科目": "他箇所聴講科目"}.get(group)
        category = _plain(props.get("科目区分", {}))
        properties = {
            "科目名": _title(name), "年度": {"number": year},
            "学期": {"select": {"name": term}}, "単位": {"number": (props.get("単位") or {}).get("number")},
            "状態": {"select": {"name": "終了"}},
            "成績履歴": {"relation": [{"id": grade["id"]}]},
        }
        if group_select:
            properties["科目群"] = {"select": {"name": group_select}}
        if category:
            properties["科目区分"] = {"select": {"name": category}}
        for source, target in (("単位要件", "単位要件"), ("GPA推移", "GPA推移")):
            relation = (props.get(source) or {}).get("relation") or []
            if relation:
                properties[target] = {"relation": [{"id": item["id"]} for item in relation]}
        planned.append({"grade_id": grade["id"], "properties": properties})
    return planned


def reconcile_academic_history(notion: Notion, state: dict, apply: bool = False) -> dict[str, int]:
    """確定済みの成績から過去授業を作り、検証できる relation を接続する。"""
    from kei_agent_course.notion_setup import AUTUMN_2026, COURSES

    sources = {key: state["databases"][key]["data_source_id"]
               for key in ("grades", "requirements", "gpa", "courses")}
    rows = {key: notion.paginate("POST", f"/data_sources/{source}/query", {"page_size": 100})
            for key, source in sources.items()}
    relation_patches = academic_relation_changes(rows)
    new_courses = historical_course_changes(rows["grades"], rows["courses"])
    current_names = {normalize_course_name(name) for name, _weekday, _period in AUTUMN_2026}
    year_patches = {row["id"]: {"年度": {"number": 2026}}
                    for row in rows["courses"]
                    if normalize_course_name(_plain(row["properties"].get("科目名", {}))) in current_names
                    and ((row["properties"].get("学期") or {}).get("select") or {}).get("name") == "秋学期"
                    and (row["properties"].get("年度") or {}).get("number") is None}
    report = {"requirements": sum("単位要件" in patch for patch in relation_patches.values()),
              "gpa": sum("GPA推移" in patch for patch in relation_patches.values()),
              "historical_courses": len(new_courses), "current_years": len(year_patches)}
    if not apply:
        return report

    source_id = sources["courses"]
    source = notion.request("GET", f"/data_sources/{source_id}")
    schemas: dict = {}
    course_props = source["properties"]
    for key in ("科目群", "学期"):
        old = course_props[key]["select"]["options"]
        wanted = COURSES["properties"][key]["select"]["options"]
        existing_names = {item["name"] for item in old}
        additions = [item for item in wanted if item["name"] not in existing_names]
        if additions:
            schemas[key] = {"select": {"options": [
                {"name": item["name"], "color": item["color"]} for item in old
            ] + additions}}
    categories = sorted({_plain(row["properties"].get("科目区分", {})) for row in rows["grades"]} - {""})
    existing_categories = (course_props.get("科目区分") or {}).get("select", {}).get("options", [])
    category_names = {item["name"] for item in existing_categories}
    new_categories = [name for name in categories if name not in category_names]
    if "科目区分" not in course_props or new_categories:
        schemas["科目区分"] = {"select": {"options": [
            {"name": item["name"], "color": item["color"]} for item in existing_categories
        ] + [{"name": name, "color": "gray"} for name in new_categories]}}
    if schemas:
        notion.request("PATCH", f"/data_sources/{source_id}", {"properties": schemas})
    for page_id, properties in relation_patches.items():
        notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
    for page_id, properties in year_patches.items():
        notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
    # 作成前に成績を読み直して、直前の relation パッチを授業ページにも反映する。
    grades = notion.paginate("POST", f"/data_sources/{sources['grades']}/query", {"page_size": 100})
    courses = notion.paginate("POST", f"/data_sources/{source_id}/query", {"page_size": 100})
    for entry in historical_course_changes(grades, courses):
        notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": source_id},
            "properties": entry["properties"],
        })
    return report


def duplicate_course_to_archive(rows: list[dict]) -> str | None:
    """中身が空の全角Ｂ行だけを、時限付きの同一科目と照合して返す。"""
    matches = [row for row in rows if normalize_course_name(
        _plain(row["properties"].get("科目名", {}))) == "情報セキュリティB"]
    if len(matches) != 2:
        return None
    scheduled = [row for row in matches if
                 (row["properties"].get("曜日", {}).get("select") or {}).get("name") == "金"
                 and row["properties"].get("時限", {}).get("number") == 2]
    other = [row for row in matches if row not in scheduled]
    if len(scheduled) != 1 or len(other) != 1:
        return None
    duplicate = other[0]
    props = duplicate["properties"]
    if _plain(props.get("科目名", {})) != "情報セキュリティＢ":
        return None
    if (props.get("曜日", {}).get("select") or {}).get("name") != "他" or props.get("時限", {}).get("number") is not None:
        return None
    for name in ("課題", "学習ログ", "成績履歴", "単位要件", "GPA推移"):
        if props.get(name, {}).get("relation"):
            return None
    for name in ("年度", "履修年次", "単位", "科目コード", "Moodle", "科目群", "必選区分"):
        value = props.get(name, {})
        if value.get("number") is not None or value.get("url") or _plain(value) or value.get("select"):
            return None
    return duplicate["id"]


@dataclass(frozen=True)
class AcademicLayoutReport:
    changed: dict[str, int]
    views: tuple[str, ...]
    archived_course: bool
    linked_grades: int = 0


def reconcile_academic_layout(notion: Notion, state: dict, apply: bool = False) -> AcademicLayoutReport:
    """全行を読んで衝突を先に検査し、表示と識別子の変更を反映する。"""
    from kei_agent_course.course_layout import (
        _upsert_view,
        course_view_payload,
        gpa_view_payload,
        grade_view_payload,
        requirement_view_payload,
    )

    databases = state["databases"]
    rows = {key: notion.paginate("POST", f"/data_sources/{databases[key]['data_source_id']}/query",
                                {"page_size": 100}) for key in ("grades", "requirements", "gpa", "courses")}
    changes = academic_page_changes({key: rows[key] for key in ("grades", "requirements", "gpa")})
    relations = academic_relation_changes(rows)
    duplicate = duplicate_course_to_archive(rows["courses"])
    if duplicate and notion.request("GET", f"/blocks/{duplicate}/children").get("results"):
        duplicate = None
    view_specs = (("grades", grade_view_payload(state), True),
                  ("requirements", requirement_view_payload(state), True),
                  ("courses", course_view_payload(state), True),
                  ("gpa", gpa_view_payload(state), False))
    if apply:
        for pages in changes.values():
            for page_id, properties in pages.items():
                notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
        for page_id, properties in relations.items():
            notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
    views = tuple(_upsert_view(notion, databases[key], payload, apply, rename_default=rename)
                  for key, payload, rename in view_specs)
    if apply and duplicate:
        notion.request("PATCH", f"/pages/{duplicate}", {"in_trash": True})
    return AcademicLayoutReport({key: len(pages) for key, pages in changes.items()}, views, bool(duplicate),
                                len(relations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent-course-academic-layout")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--history", action="store_true", help="過去授業と成績の relation を接続する")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise SystemExit(f"{TOKEN_ENV} が設定されていません")
    if args.history:
        report = reconcile_academic_history(Notion(token), read_state(), args.apply)
        print(("反映" if args.apply else "dry-run") + ": "
              + "・".join(f"{key} {count} 件" for key, count in report.items()))
        return 0
    report = reconcile_academic_layout(Notion(token), read_state(), args.apply)
    print(("反映" if args.apply else "dry-run") + ": "
          + "・".join(f"{key} {count} 件" for key, count in report.changed.items())
          + " / " + "・".join(report.views)
          + f" / 成績リンク {report.linked_grades} 件"
          + f" / 重複授業アーカイブ {int(report.archived_course)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
