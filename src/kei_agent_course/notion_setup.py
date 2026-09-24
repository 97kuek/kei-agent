"""授業ホームの正本 DB を作り、既存 schema の不足だけを補う。

研究ホームとは別のコネクト（トークン）で動かし、授業のページだけに接続する。
研究のデータベースには触れない（docs/design.md の11章）。

使い方:
    NOTION_COURSE_TOKEN=... uv run --group course kei-agent-course-setup <授業ホームのページID>

ビューの作成と表示列の統一は `kei-agent-course-layout` が担う。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError, Setup
from kei_agent_course.course_identity import normalize_course_name

# 科目の台帳。学期のあいだ変わらないもの
COURSES = {
    "icon": "📚",
    "description": "履修している科目。Moodle のカレンダーに出てくる科目名と、ここの名前をそろえる。",
    "properties": {
        "科目名": {"title": {}},
        "科目コード": {"rich_text": {}},
        "年度": {"number": {"format": "number"}},
        "履修年次": {"number": {"format": "number"}},
        "単位": {"number": {"format": "number"}},
        "科目群": {"select": {"options": [
            {"name": "A群", "color": "blue"}, {"name": "B群", "color": "green"},
            {"name": "C群", "color": "purple"}, {"name": "その他", "color": "gray"},
        ]}},
        "必選区分": {"select": {"options": [
            {"name": "必修", "color": "red"}, {"name": "選択必修", "color": "orange"},
            {"name": "選択", "color": "blue"}, {"name": "その他", "color": "gray"},
        ]}},
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
        "課題": {"title": {}},
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

GRADES = {
    "icon": "📊",
    "description": "成績 HTML から取り込んだ科目ごとの派生記録。",
    "properties": {
        "授業名": {"title": {}}, "Kei Agent 成績ID": {"rich_text": {}},
        "取得年度": {"number": {"format": "number"}}, "学期": {"select": {"options": []}},
        "単位": {"number": {"format": "number"}}, "成績": {"rich_text": {}},
        "GP": {"number": {"format": "number"}}, "科目群": {"rich_text": {}}, "科目区分": {"rich_text": {}},
    },
    "relations": {"授業": ("courses", "成績履歴")},
}

REQUIREMENTS = {
    "icon": "🎓",
    "description": "卒業要件の集計。成績との対応は根拠がある場合だけ結ぶ。",
    "properties": {
        "要件名": {"title": {}}, "Kei Agent 要件ID": {"rich_text": {}}, "大区分": {"rich_text": {}},
        "所定単位": {"number": {"format": "number"}}, "既得単位": {"number": {"format": "number"}},
        "算入単位": {"number": {"format": "number"}}, "残り単位": {"number": {"format": "number"}},
        "集計種別": {"select": {"options": []}},
    },
    "relations": {"算入成績": ("grades", "単位要件")},
}

GPA = {
    "icon": "📈",
    "description": "学期別と通算の GPA。",
    "properties": {
        "期間": {"title": {}}, "Kei Agent GPAID": {"rich_text": {}},
        "年度": {"number": {"format": "number"}}, "種別": {"select": {"options": []}},
        "GPA": {"number": {"format": "number"}},
    },
    "relations": {"対象成績": ("grades", "GPA推移")},
}

SPECS = {"courses": ("授業", COURSES), "assignments": ("課題", ASSIGNMENTS),
         "study_logs": ("学習ログ", STUDY_LOGS), "grades": ("📊 成績履歴", GRADES),
         "requirements": ("🎓 単位要件", REQUIREMENTS), "gpa": ("📈 GPA推移", GPA)}

# 既存の title property は増やさず、同じ property ID の表示名を改める。
TITLE_ALIASES = {"assignments": {"課題": "タイトル"}, "grades": {"授業名": "科目名"}}

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
    """授業ホームの下に正本6 DBを作る（すでにあれば、足りない項目だけ足す）。"""

    def run(self, courses: list[tuple[str, str, int | None]] | None = None) -> None:
        titles = [
            block["child_database"]["title"] for block in self.notion.children(self.home)
            if block["type"] == "child_database"
        ]
        canonical = {title for title, _spec in SPECS.values()}
        duplicates = sorted(title for title, count in Counter(titles).items() if title in canonical and count > 1)
        if duplicates:
            raise NotionError(f"正本データベースが重複しています: {'、'.join(duplicates)}。正本を確認してから整理してください")
        for key, (title, spec) in SPECS.items():
            self.database(key, self.home, title, spec)
        for name, weekday, period in courses or []:
            self.add_course(name, weekday, period)

    def database(self, key: str, parent: str, title: str, spec: dict) -> dict:
        """正本 schema に不足を補い、既存 title alias は同じ property ID で改名する。"""
        # Setup.database() が missing を算出する前に、既存 title property を安全に正規名へ寄せる。
        # DB が存在しない場合は親実装が title を含めて作成するので、ここでは何もしない。
        matches = [block["id"] for block in self.notion.children(parent)
                   if block["type"] == "child_database" and block["child_database"]["title"] == title]
        if len(matches) == 1 and (aliases := TITLE_ALIASES.get(key)):
            db = self.notion.request("GET", f"/databases/{matches[0]}")
            data_source_id = db["data_sources"][0]["id"]
            source = self.notion.request("GET", f"/data_sources/{data_source_id}")
            renames = {
                source["properties"][old]["id"]: {"name": canonical}
                for canonical, old in aliases.items()
                if canonical not in source["properties"]
                and old in source["properties"]
                and source["properties"][old].get("type") == "title"
            }
            if renames:
                self.notion.request("PATCH", f"/data_sources/{data_source_id}", {"properties": renames})
                self.log.append(f"タイトルを改名: {title} {list(aliases.values())} → {list(aliases)}")
        return super().database(key, parent, title, spec)

    def add_course(self, name: str, weekday: str, period: int | None = None,
                   term: str = "秋学期") -> None:
        """科目を1つ足す（同じ名前があれば何もしない）。曜日と時限は別の列に入れる。"""
        db = self.state["databases"]["courses"]
        for row in self.notion.paginate("POST", f"/data_sources/{db['data_source_id']}/query", {"page_size": 100}):
            title = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                            for part in row.get("properties", {}).get("科目名", {}).get("title") or [])
            if normalize_course_name(title) == normalize_course_name(name):
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
