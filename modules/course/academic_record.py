"""成績の記録の形（学校の部品の read_record が返す）と、保存した学務の HTML から表を拾う道具。

成績のページの読み方は学校ごとに違うので、学校の部品（schools/。書き方は school.py）に置く。ここには、どの学校でも
同じ記録の形（成績・単位要件・GPA）と、部品が使える HTML の表の読み方だけを置く。原文は残さない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


@dataclass(frozen=True)
class Grade:
    course_name: str
    year: int
    # 成績に書かれた学期の名前（授業 DB の学期への直し方は、学校の部品の course_term）
    term: str
    credits: float
    grade: str
    gp: float | None
    # 「科目群 / 科目区分」（区分が無ければ科目群だけ）
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


def tables(path: Path) -> list[list[list[str]]]:
    """HTML の表を全部。1つの表は行の並びで、1行はセルの文字の並び（空白は1つにまとめる）。"""
    text = path.read_text(encoding="utf-8", errors="strict")
    counter = _TableCounter()
    counter.feed(text)
    found = []
    for target in range(counter.count):
        parser = _TableParser(target)
        parser.feed(text)
        found.append(parser.rows)
    return found


def find_table(path: Path, header: list[str]) -> list[list[str]]:
    """1行目が header で始まる表。無ければ ValueError（保存したページが違うか、学務の画面が変わった）。"""
    for rows in tables(path):
        if rows and rows[0][: len(header)] == header:
            return rows
    raise ValueError(f"HTMLに表がありません: {path.name} / {'、'.join(header)}")


def cell_number(value: str) -> float | None:
    """セルの数。空か数でなければ None。"""
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None


def cell_year(value: str) -> int | None:
    """セルの年（4桁の数）。そうでなければ None。"""
    return int(value) if re.fullmatch(r"\d{4}", value.strip()) else None
