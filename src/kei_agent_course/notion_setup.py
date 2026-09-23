"""授業用の Notion（「授業」と「課題」の2つのデータベース）を作る。

研究ホームとは別のコネクト（トークン）で動かし、授業のページだけに接続する。
研究のデータベースには触れない（docs/design.md の11章）。

使い方:
    NOTION_COURSE_TOKEN=... uv run --group course kei-agent-course-setup <授業ホームのページID>

**ビューはここでは作れない。** Notion の公開 API はビュー（並び替え・絞り込み・列の順番）を
扱えないので、作ったばかりのデータベースは英語の `Default view` に全列が作成順で並んだ状態になる。
人が見て使える形（「時間割」を曜日→時限で並べる、「締切が近い順」で機械向けの列を右に送る、
授業ホームに表を埋め込む）は、Claude の Notion 連携から整えてある。
作り直したときは、そこも合わせて直すこと。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError, Setup

# 科目の台帳。学期のあいだ変わらないもの
COURSES = {
    "icon": "📚",
    "description": "履修している科目。Moodle のカレンダーに出てくる科目名と、ここの名前をそろえる。",
    "properties": {
        "科目名": {"title": {}},
        "科目コード": {"rich_text": {}},
        "学期": {"select": {"options": [
            {"name": "春学期", "color": "green"},
            {"name": "秋学期", "color": "orange"},
            {"name": "通年", "color": "blue"},
        ]}},
        "曜日": {"select": {"options": [
            {"name": day, "color": color} for day, color in
            [("月", "red"), ("火", "orange"), ("水", "yellow"), ("木", "green"),
             ("金", "blue"), ("土", "purple"), ("日", "pink"), ("他", "gray")]
        ]}},
        "時限": {"number": {"format": "number"}},
        "Moodle": {"url": {}},
        "状態": {"select": {"options": [
            {"name": "履修中", "color": "green"},
            {"name": "終了", "color": "gray"},
        ]}},
    },
}

# 毎週増えるもの。Moodle から取り込む
ASSIGNMENTS = {
    "icon": "📝",
    "description": "課題と締切。出どころが Moodle の行は取り込みで更新し、手で足した行は消さない。",
    "properties": {
        "タイトル": {"title": {}},
        "締切": {"date": {}},
        "状態": {"status": {"options": [
            {"name": "未着手", "color": "gray", "group": "To-do"},
            {"name": "進行中", "color": "blue", "group": "In progress"},
            {"name": "提出済み", "color": "green", "group": "Complete"},
            {"name": "期限切れ", "color": "red", "group": "Complete"},
        ]}},
        "Moodle": {"url": {}},
        "見積時間": {"number": {"format": "number"}},
        "実績時間": {"number": {"format": "number"}},
        "出どころ": {"select": {"options": [
            {"name": "Moodle", "color": "blue"},
            {"name": "手入力", "color": "gray"},
        ]}},
        # 同じ課題を二重に作らないための目印（Moodle のイベント ID）
        "Moodle ID": {"rich_text": {}},
        "最終同期": {"last_edited_time": {}},
    },
    "relations": {"科目": ("courses", "課題")},
}

STUDY_LOGS = {
    "icon": "⏱️",
    "description": "Kei Agent から記録した学習時間。記録IDで重複を防ぐ。",
    "properties": {
        "タイトル": {"title": {}},
        "Kei Agent 記録ID": {"rich_text": {}},
        "日付": {"date": {}},
        "時間（分）": {"number": {"format": "number"}},
        "メモ": {"rich_text": {}},
        "Slack": {"url": {}},
    },
    "relations": {"科目": ("courses", "学習ログ")},
}

SPECS = {"courses": ("授業", COURSES), "assignments": ("課題", ASSIGNMENTS),
         "study_logs": ("学習ログ", STUDY_LOGS)}

# 秋学期の履修（2026年度）。Moodle のカレンダーに出てくる科目名と、ここの名前をそろえる
AUTUMN_2026 = [
    ("データベース", "月", 2),
    ("情報通信ネットワークB", "月", 4),
    ("マルチメディア工学A", "火", 5),
    ("情報セキュリティB", "金", 2),
    ("マルチメディア工学B", "金", 3),
    ("次世代ネットワーク", "金", 4),
    ("統計解析実習", "他", None),
    ("プロジェクト研究B", "他", None),
]


class CourseSetup(Setup):
    """授業ホームの下に3つのデータベースを作る（すでにあれば、足りない項目だけ足す）。"""

    def run(self, courses: list[tuple[str, str, int | None]] | None = None) -> None:
        for key, (title, spec) in SPECS.items():
            self.database(key, self.home, title, spec)
        for name, weekday, period in courses or []:
            self.add_course(name, weekday, period)

    def add_course(self, name: str, weekday: str, period: int | None = None,
                   term: str = "秋学期") -> None:
        """科目を1つ足す（同じ名前があれば何もしない）。曜日と時限は別の列に入れる。"""
        db = self.state["databases"]["courses"]
        found = self.notion.request("POST", f"/data_sources/{db['data_source_id']}/query", {
            "filter": {"property": "科目名", "title": {"equals": name}}, "page_size": 1})
        if found.get("results"):
            return
        self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": db["data_source_id"]},
            "properties": {
                "科目名": {"title": [{"text": {"content": name}}]},
                "曜日": {"select": {"name": weekday}},
                "時限": {"number": period},
                "学期": {"select": {"name": term}},
                "状態": {"select": {"name": "履修中"}},
            },
        })
        self.log.append(f"科目を追加: {name}（{weekday}{period or ''}）")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-course-setup")
    parser.add_argument("home_page_id", help="授業ホームのページID（URL の末尾32文字）")
    parser.add_argument("--seed", action="store_true", help="2026年度秋学期の履修科目を入れる")
    args = parser.parse_args(argv)
    token = os.environ.get("NOTION_COURSE_TOKEN")
    if not token:
        sys.exit("NOTION_COURSE_TOKEN が設定されていません（授業用のコネクトを作って、そのトークンを入れてください）")
    state_path = Path(load_config().state_dir) / "notion-course.json"
    setup = CourseSetup(Notion(token), args.home_page_id, state_path)
    try:
        setup.run(AUTUMN_2026 if args.seed else None)
    except NotionError as e:
        sys.exit(f"Notion で失敗しました: {e}")
    finally:
        print("\n".join(setup.log) or "変更なし")
    print(f"状態: {state_path}")
    print(json.dumps({k: v.get("url") for k, v in setup.state.get("databases", {}).items()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
