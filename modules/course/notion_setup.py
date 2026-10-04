"""授業ホーム（授業時間表のページと、授業・課題・成績・単位要件の4つの DB）を作り、足りない列だけを足す。

Notion はゲートウェイ経由（client は course）で、授業ホームの中だけに届く。
研究のデータベースには触れない（docs/architecture.md）。

使い方（Notion ゲートウェイが動いていること）:
    source ~/.config/kei-agent/secrets/kei-agent.zsh   # 置き場所は config.toml の [paths] secrets
    kei-agent-module course setup [<授業ホームのページID>] [--seed <履修科目のファイル>]

列の名前と選択肢は英語で、列は決まった順に作る（docs/agents/course-agent.md の「授業ホーム（Notion）」）。
「授業」の Term の選択肢は、学校（config.toml の [course] と学校の部品。school.py）から作る。
前の形（題の列が日本語、「📊 成績履歴」など）のホームには、何も書かずに止まる。
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
from .academic_record import KIND_CATEGORY, KIND_OTHER, KIND_SUBTOTAL, KIND_TOTAL
from .course_identity import normalize_course_name
from .school import OTHER, School, SchoolError, from_config

# 授業の Status
TAKING, DONE = "Taking", "Done"
# 課題の Status
NOT_STARTED, IN_PROGRESS, SUBMITTED, OVERDUE = "Not started", "In progress", "Submitted", "Overdue"
TIMETABLE_TITLE = "授業時間表"
# 前の形の DB の名前。新しい名前の DB が無いのにこれがあれば、移し替えが済んでいない
LEGACY_TITLES = {"grades": "📊 成績履歴", "requirements": "🎓 単位要件"}


def _relation(target: str, synced: str | None = None) -> dict:
    """つながりの列。作るときに相手の data_source_id に置き換える（CourseSetup._resolve）。

    synced を渡すと相手にも戻り側の列ができる（dual_property）。渡さなければ片側だけ（single_property）。
    """
    return {"relation": {"target": target, "synced": synced}}


def _back(target: str, synced: str) -> dict:
    """相手の DB の dual_property が作る戻り側の列。setup は作らず、表の列の順番にだけ使う。"""
    return {"relation": {"back_of": target, "synced": synced}}


def _select(*names_colors: tuple[str, str]) -> dict:
    return {"select": {"options": [{"name": name, "color": color} for name, color in names_colors]}}


COURSES = {
    "icon": "📚",
    "description": "履修している科目。Name は Moodle のカレンダーの科目名とそろえる。成績とは Name＋Year＋Term で突き合わせる。",
    "properties": {
        "Name": {"title": {}},
        "Status": _select((TAKING, "green"), (DONE, "gray")),
        "Year": {"number": {"format": "number"}},
        # 選択肢は学校から作る（course_spec）
        "Term": {"select": {"options": []}},
        "Day": _select(("月", "red"), ("火", "orange"), ("水", "yellow"), ("木", "green"),
                       ("金", "blue"), ("土", "purple"), ("日", "pink"), ("他", "gray")),
        "Period": {"number": {"format": "number"}},
        "Credits": {"number": {"format": "number"}},
        "Moodle": {"url": {}},
    },
}

ASSIGNMENTS = {
    "icon": "📝",
    "description": "課題と締切。Moodle ID のある行は Moodle の取り込みで直し、Moodle ID の無い行（手入力）には触らない。",
    "properties": {
        "Name": {"title": {}},
        "Status": {"status": {"options": [
            {"name": NOT_STARTED, "color": "gray", "group": "To-do"},
            {"name": IN_PROGRESS, "color": "blue", "group": "In progress"},
            {"name": SUBMITTED, "color": "green", "group": "Complete"},
            {"name": OVERDUE, "color": "red", "group": "Complete"},
        ]}},
        "Due": {"date": {}},
        "Course": _relation("courses"),
        "Link": {"url": {}},
        # Moodle から取り込むときの照合キー（ics の UID）。空なら手入力の課題
        "Moodle ID": {"rich_text": {}},
    },
}

GRADES = {
    "icon": "📊",
    "description": "成績ページから取り込んだ科目ごとの成績。Grade が F でなければ取得済み。GPA は保存せず、GP と Credits から計算する。",
    "properties": {
        "Name": {"title": {}},
        "Grade": {"rich_text": {}},
        "GP": {"number": {"format": "number"}},
        "Credits": {"number": {"format": "number"}},
        "Category": {"rich_text": {}},
        "Group": {"rich_text": {}},
        "Course": _relation("courses"),
        "Requirement": _back("requirements", "Grades"),
        "Record ID": {"rich_text": {}},
    },
}

REQUIREMENTS = {
    "icon": "🎓",
    "description": "卒業要件の集計。Remaining が残り単位（Kind が Total の行が全体）。成績とは根拠がある場合だけ結ぶ。",
    "properties": {
        "Name": {"title": {}},
        "Remaining": {"number": {"format": "number"}},
        "Required": {"number": {"format": "number"}},
        "Counted": {"number": {"format": "number"}},
        "Group": {"rich_text": {}},
        "Kind": _select((KIND_CATEGORY, "blue"), (KIND_SUBTOTAL, "orange"), (KIND_TOTAL, "red"), (KIND_OTHER, "gray")),
        "Grades": _relation("grades", "Requirement"),
        "Record ID": {"rich_text": {}},
    },
}

# 作る順番（つながりの相手を先に作る）。ホームにもこの順に並ぶ
SPECS = {"courses": ("授業", COURSES), "assignments": ("課題", ASSIGNMENTS),
         "grades": ("成績", GRADES), "requirements": ("単位要件", REQUIREMENTS)}

# 学校から作る選択肢の色（順に使う。「その他」は灰色）
_COLORS = ("green", "orange", "blue", "pink", "purple", "yellow", "red", "brown")
# 履修科目のファイルの「曜日」に書けるもの（「他」は曜日の決まっていない科目）
_WEEKDAYS = (*WEEKDAYS, "他")


def _options(names: list[str]) -> list[dict]:
    names = list(dict.fromkeys(name for name in names if name != OTHER))
    return [{"name": name, "color": _COLORS[n % len(_COLORS)]} for n, name in enumerate(names)] + [
        {"name": OTHER, "color": "gray"}]


def course_spec(school: School) -> dict:
    """「授業」の形。Term の選択肢は学期の設定から作る。"""
    spec = copy.deepcopy(COURSES)
    spec["properties"]["Term"]["select"]["options"] = _options(list(school.terms))
    return spec


def specs(school: School) -> dict[str, tuple[str, dict]]:
    """その学校の、正本の DB の名前と形。"""
    return {**SPECS, "courses": (SPECS["courses"][0], course_spec(school))}


def _block(kind: str, text: str) -> dict:
    return {"type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def timetable_blocks(school: School) -> list[dict]:
    """授業時間表のページの中身。学校の時限の時刻を1行ずつ。分からなければ、書いてほしいと1行だけ。"""
    if not school.periods:
        return [_block("paragraph", "時限の時刻がまだ分かりません。学校の時間割を見て「1限 08:50-10:30」の形で書いてください")]
    return [_block("bulleted_list_item", f"{period}限 {start:%H:%M}-{end:%H:%M}")
            for period, (start, end) in school.periods.items()]


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
    """授業ホームの先頭に授業時間表のページを置き、4つの DB を作る（すでにあれば、足りない列だけ足す）。"""

    def __init__(self, notion, home_page_id: str, state_path: Path, school: School | None = None):
        super().__init__(notion, home_page_id, state_path)
        self.school = school or School()

    def run(self, courses: list[Course] | None = None, year: int | None = None) -> None:
        children = self.notion.children(self.home)
        self.check_home(children)
        self.timetable_page(children)
        wanted = specs(self.school)
        for key, (title, spec) in wanted.items():
            self.database(key, self.home, title, self._resolve(spec))
        for key, (_title, spec) in wanted.items():
            self.order_views(self.state["databases"][key], spec)
        for course in courses or []:
            self.add_course(course.name, course.weekday, course.period, term=course.term, year=year)

    def check_home(self, children: list[dict]) -> None:
        """書く前に、重複した DB・移し替えの済んでいない DB が無いかを見る。あれば何も書かずに止まる。"""
        wanted = specs(self.school)
        titles = Counter(b["child_database"]["title"] for b in children if b["type"] == "child_database")
        canonical = {title for title, _spec in wanted.values()}
        duplicates = sorted(title for title, count in titles.items() if title in canonical and count > 1)
        if duplicates:
            raise NotionError(f"正本データベースが重複しています: {'、'.join(duplicates)}。正本を確認してから整理してください")
        legacy = sorted(old for key, old in LEGACY_TITLES.items() if old in titles and wanted[key][0] not in titles)
        if legacy:
            raise NotionError(f"前の名前の DB があります: {'、'.join(legacy)}。列と名前を新しい形に移し替えてから setup を"
                              "動かしてください（docs/agents/course-agent.md の「授業ホーム（Notion）」）")
        for block in children:
            title = (block.get("child_database") or {}).get("title")
            if block["type"] != "child_database" or title not in canonical:
                continue
            ds_id = self.notion.request("GET", f"/databases/{block['id']}")["data_sources"][0]["id"]
            props = self.notion.request("GET", f"/data_sources/{ds_id}")["properties"]
            name = next((n for n, p in props.items() if p.get("type") == "title"), "")
            if name != "Name":
                raise NotionError(f"「{title}」の題の列が「{name}」のままです。列の名前を英語に移し替えてから setup を"
                                  "動かしてください（docs/agents/course-agent.md の「授業ホーム（Notion）」）")

    def timetable_page(self, children: list[dict]) -> str:
        """授業時間表のページ。無ければ学校の時限の時刻を書いて作る。あるページの中身には触らない。"""
        for block in children:
            if block["type"] == "child_page" and block["child_page"]["title"] == TIMETABLE_TITLE:
                return block["id"]
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": self.home},
            "icon": {"type": "emoji", "emoji": "🕘"},
            "properties": {"title": {"title": [{"text": {"content": TIMETABLE_TITLE}}]}},
            "children": timetable_blocks(self.school),
        })
        self.log.append(f"ページを作成: {TIMETABLE_TITLE}")
        return page["id"]

    def _resolve(self, spec: dict) -> dict:
        """つながりの印を、相手の data_source_id を指す列にする。戻り側の印（_back）は作らないので外す。"""
        properties = {}
        for name, value in spec["properties"].items():
            relation = value.get("relation")
            if relation is None:
                properties[name] = value
            elif "back_of" in relation:
                continue
            else:
                target = self.state["databases"][relation["target"]]["data_source_id"]
                kind = "dual_property" if relation["synced"] else "single_property"
                detail = {"synced_property_name": relation["synced"]} if relation["synced"] else {}
                properties[name] = {"relation": {"data_source_id": target, "type": kind, kind: detail}}
        return {**spec, "properties": properties}

    def order_views(self, info: dict, spec: dict) -> None:
        """表のビューの列を spec の順にそろえる（戻り側の列も含む）。spec に無い列は消さず、右に残す。"""
        live = self.notion.request("GET", f"/data_sources/{info['data_source_id']}")["properties"]
        names = [n for n in spec["properties"] if n in live] + [n for n in live if n not in spec["properties"]]
        wanted = [{"property_id": live[n]["id"], "visible": True} for n in names]
        for view in self.notion.request("GET", f"/views?database_id={info['database_id']}").get("results", []):
            full = self.notion.request("GET", f"/views/{view['id']}")
            if full.get("type") != "table":
                continue
            shown = [(p.get("property_id"), p.get("visible"))
                     for p in (full.get("configuration") or {}).get("properties", [])]
            if shown == [(p["property_id"], p["visible"]) for p in wanted]:
                continue
            self.notion.request("PATCH", f"/views/{view['id']}",
                                {"configuration": {"type": "table", "properties": wanted}})
            self.log.append(f"列の順番をそろえた: {full.get('name') or view['id']}")

    def add_course(self, name: str, weekday: str, period: int | None = None,
                   term: str = "", year: int | None = None) -> None:
        """科目を1つ足す（同じ年度・同じ名前があれば何もしない）。曜日と時限は別の列に入れる。

        年度を省くと今日の年度、学期を省くと今日の学期にする。年度が無いと、次の年も Taking の科目として出てしまう。
        """
        today = date.today()
        year = year or self.school.academic_year(today)
        term = term or self.school.term_of(today) or OTHER
        db = self.state["databases"]["courses"]
        for row in self.notion.paginate("POST", f"/data_sources/{db['data_source_id']}/query", {"page_size": 100}):
            props = row.get("properties", {})
            if notion_props.select(props.get("Status")) == DONE:
                continue
            if notion_props.number(props.get("Year")) not in (None, year):
                continue
            if normalize_course_name(notion_props.plain(props.get("Name"))) == normalize_course_name(name):
                return
        self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": db["data_source_id"]},
            "properties": {
                "Name": notion_props.title(name),
                "Status": {"select": {"name": TAKING}},
                "Year": {"number": year},
                "Term": {"select": {"name": term}},
                "Day": {"select": {"name": weekday}},
                "Period": {"number": period},
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
