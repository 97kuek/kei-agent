"""早稲田大学（大学のモジュールの学校の部品。config.toml の [course] に school = "waseda"）。

時限の時刻・学期の既定と、成績の取り込み方（保存した成績と単位の HTML を読む）。部品に置けるものは school.py に。
時限の時刻が学部や年度で違うときは、config.toml の [course.periods] で上書きする。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..academic_record import AcademicRecord, GPAEntry, Grade, Requirement, cell_number, cell_year, find_table

LABEL = "早稲田大学"
# 標準の時間割（1時限 100 分）
PERIODS = {
    1: "08:50-10:30",
    2: "10:40-12:20",
    3: "13:10-14:50",
    4: "15:05-16:45",
    5: "17:00-18:40",
    6: "18:55-20:35",
    7: "20:45-22:25",
}
SPRING, AUTUMN, ALL_YEAR = "春学期", "秋学期", "通年"
_SPRING_MONTHS = [4, 5, 6, 7, 8]
_AUTUMN_MONTHS = [9, 10, 11, 12, 1, 2, 3]
# 学期 → 開かれる月（授業 DB の「学期」の選択肢と同じ名前）。クォーター科目は、含まれる学期のあいだ履修中として扱う
TERMS = {
    SPRING: _SPRING_MONTHS,
    AUTUMN: _AUTUMN_MONTHS,
    ALL_YEAR: _SPRING_MONTHS + _AUTUMN_MONTHS,
    "春ク": _SPRING_MONTHS,
    "夏ク": _SPRING_MONTHS,
    "秋ク": _AUTUMN_MONTHS,
    "冬ク": _AUTUMN_MONTHS,
}

# 成績の取り込み（kei-agent-module course academic-import）

# 成績 HTML の学期名 → 授業 DB の学期名（クォーターと通年は、そのまま）
_GRADE_TERMS = {"春期": SPRING, "秋期": AUTUMN}
# 成績 HTML で学期として読む名前（ほかは「その他」）
_GRADE_TERM_NAMES = frozenset({*_GRADE_TERMS, "春ク", "夏ク", "秋ク", "冬ク", ALL_YEAR})
# 成績の学期 → GPA の期間。夏ク・秋ク・通年は公表値と照合済み。春ク・冬クは確かめていないので結ばない
_GPA_TERMS = {**_GRADE_TERMS, "夏ク": SPRING, "秋ク": AUTUMN, ALL_YEAR: AUTUMN}
# GPA の表に出る期間の並び（年度ごとに春学期・秋学期の順）
GPA_KINDS = (SPRING, AUTUMN)
# 科目群の表記（成績 HTML → 授業 DB の選択肢）
COURSE_GROUPS = {"Ａ群": "A群", "Ｂ群": "B群", "Ｃ群(専門教育科目)": "C群", "他箇所聴講科目": "他箇所聴講科目"}
# 単位要件の名前が、成績の区分と違うもの（学務の表記を確かめて対応づけたものだけ）
_REQUIREMENT_NAMES = {
    ("Ｃ群(専門教育科目)", "専門選択必修"): ("専門選択必修（学系別専門）",),
    ("Ａ群", "外国語 英語"): ("外国語 英語 必修",),
    ("Ｂ群", "自然科学 物理学"): ("自然科学 物理学 必修",),
    ("Ｂ群", "自然科学 化学"): ("自然科学 化学 必修",),
}


def read_record(files: list[Path]) -> AcademicRecord:
    """成績の HTML（科目ごとの成績の表）と、単位の HTML（単位要件と GPA の表）を、この順に2つ読む。"""
    if len(files) != 2:
        raise ValueError("早稲田の成績は、成績の HTML と単位の HTML の2つを、この順に渡してください")
    grades_html, credits_html = files
    return AcademicRecord(_grades(grades_html), _requirements(credits_html), _gpa(credits_html))


def course_term(term: str) -> str:
    """成績の学期名を、授業 DB の学期名にそろえる（春期→春学期。クォーターと通年はそのまま）。"""
    return _GRADE_TERMS.get(term, term)


def gpa_kind(term: str, year: int | None, course: str) -> str | None:
    """その成績が入る GPA の期間。分からなければ None（結ばない）。"""
    # 成績の「その他」は学期を示さない。データ科学入門αの2025年度だけは、春学期の公表値との単位加重計算で照合済み
    if term == "その他" and year == 2025 and course.startswith("データ科学入門α "):
        return SPRING
    return _GPA_TERMS.get(term)


def requirement_names(group: str, category: str) -> tuple[str, ...]:
    """成績の区分に当たる単位要件の名前の候補（前から順に探す）。"""
    return (category, *_REQUIREMENT_NAMES.get((group, category), ()))


def _grades(path: Path) -> tuple[Grade, ...]:
    rows = find_table(path, ["科目名", "取得年度", "学期", "単位", "成績", "ＧＰ"])
    result: list[Grade] = []
    group = ""
    subcategory = ""
    for row in rows[1:]:
        if len(row) < 6 or not row[0]:
            continue
        name, year_text, term, credits_text, grade, gp_text = row[:6]
        year = cell_year(year_text)
        credits = cell_number(credits_text)
        if year is None or credits is None:
            if name.startswith("◎"):
                group = name.strip("◎").strip()
            elif name.startswith(("【", "《")):
                subcategory = name.strip("【】《》").strip()
            continue
        result.append(Grade(
            course_name=name,
            year=year,
            term=term if term in _GRADE_TERM_NAMES else "その他",
            credits=credits,
            grade=grade,
            gp=cell_number(gp_text),
            category=" / ".join(x for x in (group, subcategory) if x),
        ))
    return tuple(result)


def _requirements(path: Path) -> tuple[Requirement, ...]:
    rows = find_table(path, ["科目区分名", "所定", "既得", "算入"])
    result: list[Requirement] = []
    current_group = ""
    for row in rows[1:]:
        if not row:
            continue
        numbers = [cell_number(value) for value in row[-3:]]
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
    rows = find_table(path, ["年度", "GPA"])
    result: list[GPAEntry] = []
    current_year: int | None = None
    term_index = 0
    for row in rows[1:]:
        if not row:
            continue
        if re.fullmatch(r"\d{4}年度", row[0]):
            current_year = int(row[0][:4])
            term_index = 0
        value = cell_number(row[-1])
        if value is None:
            continue
        if row[0] == "通算":
            result.append(GPAEntry("通算", 0, value, "通算"))
            continue
        if current_year is None:
            raise ValueError("GPA表の年度が見つかりません")
        kind = SPRING if term_index == 0 else AUTUMN
        term_index += 1
        result.append(GPAEntry(f"{current_year}年度（{kind}）", current_year, value, kind))
    return tuple(result)
