"""授業ホームの Notion view と既存課題ページの見せ方を安全に統一する。"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from kei_agent.notion import Notion
from kei_agent_course.notion_sync import TOKEN_ENV, read_state

_ASSIGNMENT_COLUMNS = ("科目", "課題", "締切", "状態")


@dataclass(frozen=True)
class LayoutReport:
    """dry-run と apply が同じ形式で返す view 作成・更新予定。"""

    planned: tuple[str, ...]


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
    found = notion.request("GET", f"/views?database_id={database['database_id']}").get("results") or []
    existing = next((view for view in found if view.get("name") == payload["name"]), None)
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


def apply_layout(notion: Notion, state: dict, apply: bool = False) -> LayoutReport:
    """課題・GPA view の変更予定を返し、`apply=True` のときだけ Notion に書き込む。"""
    databases = state["databases"]
    planned = (
        _upsert_view(notion, databases["assignments"], assignment_view_payload(state), apply),
        _upsert_view(notion, databases["gpa"], gpa_view_payload(state), apply),
    )
    return LayoutReport(planned)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent-course-layout")
    parser.add_argument("--apply", action="store_true", help="Notion の課題・GPA view を実際に更新する")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} が設定されていません")
    report = apply_layout(Notion(token), read_state(), apply=args.apply)
    mode = "反映" if args.apply else "dry-run"
    print(f"{mode}: " + "・".join(report.planned))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
