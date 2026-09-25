"""早稲田の保存済み成績HTMLを、Notionへ渡せる派生データにする。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from kei_agent_course.periods import GRADE_TERM_NAMES


@dataclass(frozen=True)
class Grade:
    course_name: str
    year: int
    term: str
    credits: float
    grade: str
    gp: float | None
    category: str


@dataclass(frozen=True)
class Requirement:
    name: str
    group: str
    required: float
    earned: float
    included: float
    remaining: float
    kind: str


@dataclass(frozen=True)
class GPAEntry:
    period: str
    year: int
    gpa: float
    kind: str


@dataclass(frozen=True)
class AcademicRecord:
    grades: tuple[Grade, ...]
    requirements: tuple[Requirement, ...]
    gpa: tuple[GPAEntry, ...]


class _TableParser(HTMLParser):
    """指定した table 番号の直接の行だけを拾う（学務HTMLは入れ子が深い）。"""

    def __init__(self, target: int):
        super().__init__()
        self.target = target
        self.stack: list[int] = []
        self.next_id = 0
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.stack.append(self.next_id)
            self.next_id += 1
        elif tag == "tr" and self.stack and self.stack[-1] == self.target:
            self.row = []
            self.cell = None
        elif tag in ("td", "th") and self.row is not None and self.stack and self.stack[-1] == self.target:
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.stack and self.stack[-1] == self.target:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.stack:
            self.stack.pop()


class _TableCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.count += 1


def _tables(path: Path) -> list[list[list[str]]]:
    text = path.read_text(encoding="utf-8", errors="strict")
    counter = _TableCounter()
    counter.feed(text)
    found = []
    for target in range(counter.count):
        parser = _TableParser(target)
        parser.feed(text)
        found.append(parser.rows)
    return found


def _find_table(path: Path, header: list[str]) -> list[list[str]]:
    for rows in _tables(path):
        if rows and rows[0][: len(header)] == header:
            return rows
    raise ValueError(f"HTMLに表がありません: {path.name} / {'、'.join(header)}")


def _number(value: str) -> float | None:
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None


def _year(value: str) -> int | None:
    return int(value) if re.fullmatch(r"\d{4}", value.strip()) else None


def _grades(path: Path) -> tuple[Grade, ...]:
    rows = _find_table(path, ["科目名", "取得年度", "学期", "単位", "成績", "ＧＰ"])
    result: list[Grade] = []
    group = ""
    subcategory = ""
    for row in rows[1:]:
        if len(row) < 6 or not row[0]:
            continue
        name, year_text, term, credits_text, grade, gp_text = row[:6]
        year = _year(year_text)
        credits = _number(credits_text)
        if year is None or credits is None:
            if name.startswith("◎"):
                group = name.strip("◎").strip()
            elif name.startswith(("【", "《")):
                subcategory = name.strip("【】《》").strip()
            continue
        result.append(Grade(
            course_name=name,
            year=year,
            term=term if term in GRADE_TERM_NAMES else "その他",
            credits=credits,
            grade=grade,
            gp=_number(gp_text),
            category=" / ".join(x for x in (group, subcategory) if x),
        ))
    return tuple(result)


def _requirements(path: Path) -> tuple[Requirement, ...]:
    rows = _find_table(path, ["科目区分名", "所定", "既得", "算入"])
    result: list[Requirement] = []
    current_group = ""
    for row in rows[1:]:
        if not row:
            continue
        numbers = [_number(value) for value in row[-3:]]
        if not any(value is not None for value in numbers):
            continue
        labels = row[:-3]
        if len(labels) >= 2:
            group, name = labels[0].strip(), labels[1].strip()
        elif labels:
            group, name = "", labels[0].strip()
        else:
            group, name = "", ""
        if not group:
            group = current_group
        if group:
            current_group = group
        if not name:
            name = group or "（名称なし）"
        required, earned, included = (value or 0 for value in numbers)
        kind = "総合計" if name == "総合計" else "小計" if "小計" in name else "区分"
        result.append(Requirement(name, group, required, earned, included, max(required - included, 0), kind))
    return tuple(result)


def _gpa(path: Path) -> tuple[GPAEntry, ...]:
    rows = _find_table(path, ["年度", "GPA"])
    result: list[GPAEntry] = []
    current_year: int | None = None
    term_index = 0
    for row in rows[1:]:
        if not row:
            continue
        if re.fullmatch(r"\d{4}年度", row[0]):
            current_year = int(row[0][:4])
            term_index = 0
        value = _number(row[-1])
        if value is None:
            continue
        if row[0] == "通算":
            result.append(GPAEntry("通算", 0, value, "通算"))
            continue
        if current_year is None:
            raise ValueError("GPA表の年度が見つかりません")
        kind = "春学期" if term_index == 0 else "秋学期"
        term_index += 1
        result.append(GPAEntry(f"{current_year}年度（{kind}）", current_year, value, kind))
    return tuple(result)


def parse_academic_record(grades_html: Path, credits_html: Path) -> AcademicRecord:
    """2つのHTMLを読み、原文を保持しない成績レコードに変換する。"""
    return AcademicRecord(_grades(grades_html), _requirements(credits_html), _gpa(credits_html))
