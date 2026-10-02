"""授業ホームの正本 DB を作り、既存 schema の不足だけを補う。

Notion はゲートウェイ経由（client は course）で、授業ホームの中だけに届く。
研究のデータベースには触れない（docs/architecture.md）。

使い方（Notion ゲートウェイが動いていること）:
    source ~/.config/kei-agent/secrets/kei-agent.zsh   # 置き場所は config.toml の [paths] secrets
    kei-agent-module course setup [<授業ホームのページID>] [--seed <履修科目のファイル>]

「授業」の「学期」と「科目群」の選択肢は、学校（config.toml の [course] と学校の部品。school.py）から作る。
履修科目のファイルの書き方は、同じフォルダの courses.example.toml。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from kei_agent_a2a.api import WEEKDAYS, NotionError, Setup, gateway_notion, load_config

from . import notion_props
from .course_identity import normalize_course_name
from .school import OTHER, School, SchoolError, from_config

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
        # 選択肢は学校から作る（course_spec）
        "科目群": {"select": {"options": []}},
        "科目区分": {"select": {"options": []}},
        "必選区分": {"select": {"options": [
            {"name": "必修", "color": "red"}, {"name": "選択必修", "color": "orange"},
            {"name": "選択", "color": "blue"}, {"name": "その他", "color": "gray"},
        ]}},
        "学期": {"select": {"options": []}},
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
         "grades": ("📊 成績履歴", GRADES),
         "requirements": ("🎓 単位要件", REQUIREMENTS), "gpa": ("📈 GPA推移", GPA)}

# 既存の title property は増やさず、同じ property ID の表示名を改める。
TITLE_ALIASES = {"assignments": {"課題": "タイトル"}, "grades": {"授業名": "科目名"}}

# 学校から作る選択肢の色（順に使う。「その他」は灰色）
_COLORS = ("green", "orange", "blue", "pink", "purple", "yellow", "red", "brown")
# 履修科目のファイルの「曜日」に書けるもの（「他」は曜日の決まっていない科目）
_WEEKDAYS = (*WEEKDAYS, "他")


def _options(names: list[str]) -> list[dict]:
    names = list(dict.fromkeys(name for name in names if name != OTHER))
    return [{"name": name, "color": _COLORS[n % len(_COLORS)]} for n, name in enumerate(names)] + [
        {"name": OTHER, "color": "gray"}]


def course_spec(school: School) -> dict:
    """「授業」の形。「学期」の選択肢は学期の設定から、「科目群」は学校の部品から作る。"""
    spec = copy.deepcopy(COURSES)
    spec["properties"]["学期"]["select"]["options"] = _options(list(school.terms))
    spec["properties"]["科目群"]["select"]["options"] = _options(list(school.course_groups))
    return spec


def specs(school: School) -> dict[str, tuple[str, dict]]:
    """その学校の、正本の DB の名前と形。"""
    return {**SPECS, "courses": (SPECS["courses"][0], course_spec(school))}


@dataclass(frozen=True)
class Course:
    """履修科目のファイルの1行。学期を省くと、ファイルの term（それも無ければ今日の学期）。"""
    name: str
    weekday: str
    period: int | None = None
    term: str = ""


def read_seed(path: Path) -> tuple[int | None, list[Course]]:
    """履修科目のファイル（courses.example.toml の形）を読む。年度（書かなければ None）と科目の並び。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"履修科目のファイルを読めません: {e}") from None
    year, term, rows = data.get("year"), data.get("term", ""), data.get("courses")
    if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
        raise ValueError(f"{path.name} の year は年度の数にしてください（例: year = 2026）")
    if not isinstance(term, str) or not isinstance(rows, list) or not rows:
        raise ValueError(f"{path.name} には courses（科目の並び）を書いてください。書き方は courses.example.toml")
    found = []
    for n, row in enumerate(rows, 1):
        row = row if isinstance(row, dict) else {}
        name, weekday, period = row.get("name"), row.get("weekday", "他"), row.get("period")
        own_term = row.get("term", term)
        if (not isinstance(name, str) or not name.strip() or weekday not in _WEEKDAYS
                or not (period is None or (isinstance(period, int) and not isinstance(period, bool)))
                or not isinstance(own_term, str)):
            raise ValueError(f"{path.name} の {n} 件目: name（科目名）、weekday（月〜日か「他」）、"
                             "period（時限の数。無ければ省く）、term（学期）の形で書いてください")
        found.append(Course(name.strip(), weekday, period, own_term))
    return year, found


class CourseSetup(Setup):
    """授業ホームの下に正本の5つの DB を作る（すでにあれば、足りない項目だけ足す）。"""

    def __init__(self, notion, home_page_id: str, state_path: Path, school: School | None = None):
        super().__init__(notion, home_page_id, state_path)
        self.school = school or School()

    def run(self, courses: list[Course] | None = None, year: int | None = None) -> None:
        titles = [
            block["child_database"]["title"] for block in self.notion.children(self.home)
            if block["type"] == "child_database"
        ]
        wanted = specs(self.school)
        canonical = {title for title, _spec in wanted.values()}
        duplicates = sorted(title for title, count in Counter(titles).items() if title in canonical and count > 1)
        if duplicates:
            raise NotionError(f"正本データベースが重複しています: {'、'.join(duplicates)}。正本を確認してから整理してください")
        for key, (title, spec) in wanted.items():
            self.database(key, self.home, title, spec)
        for course in courses or []:
            self.add_course(course.name, course.weekday, course.period, term=course.term, year=year)

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
                   term: str = "", year: int | None = None) -> None:
        """科目を1つ足す（同じ年度・同じ名前があれば何もしない）。曜日と時限は別の列に入れる。

        年度を省くと今日の年度、学期を省くと今日の学期にする。年度が無いと、次の年も「履修中」の科目として出てしまう。
        """
        today = date.today()
        year = year or self.school.academic_year(today)
        term = term or self.school.term_of(today) or OTHER
        db = self.state["databases"]["courses"]
        for row in self.notion.paginate("POST", f"/data_sources/{db['data_source_id']}/query", {"page_size": 100}):
            props = row.get("properties", {})
            if notion_props.select(props.get("状態")) == "終了":
                continue
            if notion_props.number(props.get("年度")) not in (None, year):
                continue
            if normalize_course_name(notion_props.plain(props.get("科目名"))) == normalize_course_name(name):
                return
        self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": db["data_source_id"]},
            "properties": {
                "科目名": notion_props.title(name),
                "年度": {"number": year},
                "曜日": {"select": {"name": weekday}},
                "時限": {"number": period},
                "学期": {"select": {"name": term}},
                "状態": {"select": {"name": "履修中"}},
            },
        })
        self.log.append(f"科目を追加: {name}（{year}年度 {weekday}{period or ''}）")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-module course setup")
    parser.add_argument("home_page_id", nargs="?", default="",
                        help="授業ホームのページID（省くと agents.csv の course の行の notion）")
    parser.add_argument("--seed", type=Path, metavar="ファイル",
                        help="履修科目のファイル（書き方は modules/course/courses.example.toml）の科目を「授業」に入れる")
    args = parser.parse_args(argv)
    config = load_config()
    home = args.home_page_id or config.notion.course_home
    if not home:
        sys.exit("授業ホームのページ ID がありません（agents.csv の course の行の notion）")
    try:
        school = from_config(config)
        year, courses = read_seed(args.seed.expanduser()) if args.seed else (None, None)
    except (SchoolError, ValueError) as e:
        sys.exit(str(e))
    try:
        notion = gateway_notion("course", config=config)
    except NotionError as e:
        sys.exit(str(e))
    state_path = Path(config.state_dir) / "notion-course.json"
    setup = CourseSetup(notion, home, state_path, school)
    try:
        setup.run(courses, year=year)
    except NotionError as e:
        sys.exit(f"Notion で失敗しました: {e}")
    finally:
        print("\n".join(setup.log) or "変更なし")
    print(f"状態: {state_path}")
    print(json.dumps({k: v.get("url") for k, v in setup.state.get("databases", {}).items()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
