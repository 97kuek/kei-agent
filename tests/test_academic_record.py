from pathlib import Path

from kei_agent_course.academic_record import parse_academic_record


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

    record = parse_academic_record(grades, credits)

    assert len(record.grades) == 1
    assert record.grades[0].course_name == "数学"
    assert record.grades[0].category == "Ａ群 / 基礎科目"
    assert record.grades[0].gp == 4.0
    assert record.requirements[-1].name == "総合計"
    assert record.requirements[-1].remaining == 5.0
    assert [entry.kind for entry in record.gpa] == ["春学期", "秋学期", "通算"]
    assert record.gpa[-1].gpa == 3.4
