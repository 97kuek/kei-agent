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
    terms = {"春期": "春学期", "秋期": "秋学期"}
    for page in rows["grades"]:
        props = page["properties"]
        group = _plain(props.get("科目群", {}))
        category = _plain(props.get("科目区分", {}))
        if not group and " / " in category:
            group, category = category.split(" / ", 1)
        patch: dict = {}
        if not props.get("単位要件", {}).get("relation"):
            target = requirements.get((group, category), [])
            if group and category and len(target) == 1:
                patch["単位要件"] = {"relation": [{"id": target[0]}]}
        if not props.get("GPA推移", {}).get("relation"):
            kind = terms.get((props.get("学期", {}).get("select") or {}).get("name"))
            target = gpas.get((props.get("取得年度", {}).get("number"), kind), []) if kind else []
            if len(target) == 1:
                patch["GPA推移"] = {"relation": [{"id": target[0]}]}
        if patch:
            changes[page["id"]] = patch
    return changes


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
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise SystemExit(f"{TOKEN_ENV} が設定されていません")
    report = reconcile_academic_layout(Notion(token), read_state(), args.apply)
    print(("反映" if args.apply else "dry-run") + ": "
          + "・".join(f"{key} {count} 件" for key, count in report.changed.items())
          + " / " + "・".join(report.views)
          + f" / 成績リンク {report.linked_grades} 件"
          + f" / 重複授業アーカイブ {int(report.archived_course)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
