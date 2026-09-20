"""授業用の Notion（「授業」と「課題」の2つのデータベース）を作る。

研究ホームとは別のコネクト（トークン）で動かし、授業のページだけに接続する。
研究のデータベースには触れない（docs/plan.md の15章）。

使い方:
    NOTION_COURSE_TOKEN=... uv run --group course kei-agent-course-setup <授業ホームのページID>
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
        "曜日時限": {"rich_text": {}},
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

SPECS = {"courses": ("授業", COURSES), "assignments": ("課題", ASSIGNMENTS)}


class CourseSetup(Setup):
    """授業ホームの下に、2つのデータベースを作る（すでにあれば、足りない項目だけ足す）。"""

    def run(self) -> None:
        for key, (title, spec) in SPECS.items():
            self.database(key, self.home, title, spec)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-course-setup")
    parser.add_argument("home_page_id", help="授業ホームのページID（URL の末尾32文字）")
    args = parser.parse_args(argv)
    token = os.environ.get("NOTION_COURSE_TOKEN")
    if not token:
        sys.exit("NOTION_COURSE_TOKEN が設定されていません（授業用のコネクトを作って、そのトークンを入れてください）")
    state_path = Path(load_config().state_dir) / "notion-course.json"
    setup = CourseSetup(Notion(token), args.home_page_id, state_path)
    try:
        setup.run()
    except NotionError as e:
        sys.exit(f"Notion で失敗しました: {e}")
    finally:
        print("\n".join(setup.log) or "変更なし")
    print(f"状態: {state_path}")
    print(json.dumps({k: v.get("url") for k, v in setup.state.get("databases", {}).items()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
