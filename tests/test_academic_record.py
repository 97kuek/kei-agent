"""早稲田の部品（schools/waseda.py）の成績の読み方。保存した成績と単位の HTML から、原文を残さない記録を作る。"""

from pathlib import Path

import pytest

from kei_agent_modules.course.schools import waseda


def test_parse_waseda_grade_and_credit_tables(tmp_path: Path):
    grades = tmp_path / "grades.html"
    grades.write_text(
        """<html><body><table><tr><td><table>
        <tr><th>科目名</th><th>取得年度</th><th>学期</th><th>単位</th><th>成績</th><th>ＧＰ</th></tr>
        <tr><td>◎Ａ群◎</td><td></td><td></td><td></td><td></td><td></td></tr>
        <tr><td>【基礎科目】</td><td></td><td></td><td></td><td></td><td></td></tr>
        <tr><td>数学</td><td>2026</td><td>春期</td><td>2</td><td>A+</td><td>4</td></tr>
        </table></td></tr></table></body></html>""",
        encoding="utf-8",
    )
    credits = tmp_path / "credits.html"
    credits.write_text(
        """<html><body><table><tr><td><table>
        <tr><th>科目区分名</th><th>所定</th><th>既得</th><th>算入</th></tr>
        <tr><td>Ｃ群</td><td>専門必修</td><td>10</td><td>8</td><td>8</td></tr>
        <tr><td>総合計</td><td></td><td>20</td><td>15</td><td>15</td></tr>
        </table></td></tr></table>
        <table><tr><td><table>
        <tr><th>年度</th><th>GPA</th></tr>
        <tr><td rowspan="2">2026年度</td><td>3.50</td></tr>
        <tr><td>3.20</td></tr>
        <tr><td>通算</td><td>3.40</td></tr>
        </table></td></tr></table></body></html>""",
        encoding="utf-8",
    )

    record = waseda.read_record([grades, credits])

    grade, = record.grades
    assert (grade.course_name, grade.category, grade.gp) == ("数学", "Ａ群 / 基礎科目", 4.0)
    assert [(r.name, r.kind) for r in record.requirements] == [("専門必修", "Category"), ("総合計", "Total")]
    assert (record.requirements[-1].included, record.requirements[-1].remaining) == (15.0, 5.0)
    assert not hasattr(record, "gpa")


def test_waseda_needs_the_grade_and_credit_pages_in_order(tmp_path: Path):
    with pytest.raises(ValueError, match="成績の HTML と単位の HTML の2つ"):
        waseda.read_record([tmp_path / "grades.html"])
