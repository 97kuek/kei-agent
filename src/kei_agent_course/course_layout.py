"""授業ホームの Notion view と既存課題ページの見せ方を安全に統一する。"""

from __future__ import annotations

import argparse
import os
import urllib.parse
from dataclasses import dataclass

from kei_agent.notion import MAX_PAGES, Notion, NotionError
from kei_agent_course.notion_sync import TOKEN_ENV, assignment_template_blocks, assignment_title, read_state

_ASSIGNMENT_COLUMNS = ("科目", "課題", "締切", "状態")


@dataclass(frozen=True)
class LayoutReport:
    """dry-run と apply が同じ形式で返す view 作成・更新予定。"""

    planned: tuple[str, ...]
    assignments: AssignmentLayoutReport | None = None


@dataclass(frozen=True)
class AssignmentLayoutReport:
    """既存課題の title 値と空ページに対する、安全な移行予定・実績。"""

    renamed: int
    templated: int


def assignment_view_payload(state: dict) -> dict:
    """課題を利用者が見る順序だけで表示し、締切順にする table view。"""
    properties = state["databases"]["assignments"]["properties"]
    ordered = [*(_ASSIGNMENT_COLUMNS), *(name for name in properties if name not in _ASSIGNMENT_COLUMNS)]
    return {
        "name": "課題一覧",
        "type": "table",
        "configuration": {
            "type": "table",
            "properties": [
                {"property_id": properties[name], "visible": name in _ASSIGNMENT_COLUMNS}
                for name in ordered
            ],
        },
        "sorts": [{"property": properties["締切"], "direction": "ascending"}],
    }


def gpa_view_payload(state: dict) -> dict:
    """既存の GPA 記録を補完せず、そのまま期間順の line chart にする。"""
    properties = state["databases"]["gpa"]["properties"]
    return {
        "name": "GPA推移",
        "type": "chart",
        "configuration": {
            "type": "chart",
            "chart_type": "line",
            "x_axis_property_id": properties["期間"],
            "y_axis_property_id": properties["GPA"],
            "sort": "x_ascending",
            "color_theme": "blue",
            "height": "medium",
            "axis_labels": "both",
            "grid_lines": "horizontal",
            "show_data_labels": True,
            "smooth_line": False,
            "hide_line_fill_area": False,
        },
        "sorts": [{"property": properties["年度"], "direction": "ascending"}],
    }


def _upsert_view(notion: Notion, database: dict, payload: dict, apply: bool) -> str:
    """同じ名前の view があれば更新し、なければ作る。dry-run では GET 以外を送らない。"""
    found = _views(notion, database["database_id"])
    existing = None
    for view in found:
        # List views の応答は name を省略するため、個別取得した完全な view で照合する。
        full_view = view if view.get("name") else notion.request("GET", f"/views/{view['id']}")
        if full_view.get("name") == payload["name"]:
            existing = full_view
            break
    action = "update" if existing else "create"
    if apply:
        if existing:
            notion.request("PATCH", f"/views/{existing['id']}", payload)
        else:
            notion.request("POST", "/views", {
                "database_id": database["database_id"],
                "data_source_id": database["data_source_id"],
                **payload,
            })
    return f"{payload['name']}: {action}"


def _views(notion: Notion, database_id: str) -> list[dict]:
    """List views の全ページを読む。後続ページの同名 view も重複作成しない。"""
    found: list[dict] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        query = {"database_id": database_id, "page_size": 100}
        if cursor:
            query["start_cursor"] = cursor
        response = notion.request("GET", f"/views?{urllib.parse.urlencode(query)}")
        found.extend(response.get("results") or [])
        cursor = response.get("next_cursor")
        if not response.get("has_more") or not cursor:
            return found
    raise NotionError(f"GET /views: ページが多すぎます（{MAX_PAGES} ページで打ち切り）")


def _plain_title(prop: dict) -> str:
    """Notion の title 応答を、比較だけに使う文字列へ戻す。"""
    return "".join(part.get("plain_text") or part.get("text", {}).get("content") or ""
                   for part in prop.get("title") or [])


def reconcile_assignment_pages(notion: Notion, state: dict, apply: bool = False) -> AssignmentLayoutReport:
    """既存課題の厳密な Moodle title と空本文だけを整理する。"""
    database = state["databases"]["assignments"]
    rows = notion.paginate("POST", f"/data_sources/{database['data_source_id']}/query", {"page_size": 100})
    renamed = templated = 0
    for row in rows:
        page_id = row["id"]
        current = _plain_title((row.get("properties") or {}).get("課題") or {})
        normalized = assignment_title(current)
        if normalized != current:
            renamed += 1
            if apply:
                notion.request("PATCH", f"/pages/{page_id}", {
                    "properties": {"課題": {"title": [{"text": {"content": normalized}}]}},
                })
        children = notion.request("GET", f"/blocks/{page_id}/children").get("results") or []
        if not children:
            templated += 1
            if apply:
                notion.request("PATCH", f"/blocks/{page_id}/children", {"children": assignment_template_blocks()})
    return AssignmentLayoutReport(renamed, templated)


def apply_layout(notion: Notion, state: dict, apply: bool = False) -> LayoutReport:
    """課題・GPA view の変更予定を返し、`apply=True` のときだけ Notion に書き込む。"""
    databases = state["databases"]
    planned = (
        _upsert_view(notion, databases["assignments"], assignment_view_payload(state), apply),
        _upsert_view(notion, databases["gpa"], gpa_view_payload(state), apply),
    )
    return LayoutReport(planned, reconcile_assignment_pages(notion, state, apply))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent-course-layout")
    parser.add_argument("--apply", action="store_true", help="Notion の課題・GPA view を実際に更新する")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} が設定されていません")
    report = apply_layout(Notion(token), read_state(), apply=args.apply)
    mode = "反映" if args.apply else "dry-run"
    assignment_changes = report.assignments or AssignmentLayoutReport(0, 0)
    print(f"{mode}: " + "・".join(report.planned)
          + f" / 課題名 {assignment_changes.renamed} 件・空ページ整理 {assignment_changes.templated} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
