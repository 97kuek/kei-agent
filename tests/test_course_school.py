"""学校ごとの違い（modules/course/school.py と schools/）。時限の時刻・学期は設定、成績の取り込み方は学校の部品。"""

from datetime import date

import pytest

from kei_agent.configuration.config import load_config
from kei_agent_modules.course import notion_setup, school
from kei_agent_modules.course.academic_record import AcademicRecord


def _home(tmp_path, config: str):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(config, encoding="utf-8")
    return home


def test_waseda_is_bundled_and_chosen_in_the_config(tmp_path):
    config = load_config(env={"KEI_AGENT_HOME": str(_home(tmp_path, '[course]\nschool = "waseda"\n'))})
    waseda = school.from_config(config)
    assert "waseda" in school.bundled() and (waseda.name, waseda.label) == ("waseda", "早稲田大学")
    assert waseda.span(1)[0].isoformat(timespec="minutes") == "08:50" and waseda.term_of(date(2026, 9, 28)) == "秋学期"


def test_without_a_school_nothing_is_guessed(tmp_path):
    """学校を選ばなければ、時限の時刻は分からず、学期で科目を隠さない（年度は4月始まり）。"""
    none = school.from_config(load_config(env={"KEI_AGENT_HOME": str(_home(tmp_path, ""))}))
    assert none.periods == {} and none.at(date(2026, 9, 28), 2) is None
    assert none.in_term("春学期", date(2026, 11, 9)) and none.term_of(date(2026, 9, 28)) == ""
    assert none.academic_year(date(2027, 3, 31)) == 2026
    with pytest.raises(school.SchoolError, match="学校の部品が選ばれていません"):
        none.read_record([])


def test_period_times_and_terms_in_the_config_replace_the_schools_own(tmp_path):
    """学部で時刻が違う、前期・後期で数える学校、などは設定で書き換える（学校の部品の既定を置き換える）。"""
    home = _home(tmp_path, '[course]\nschool = "waseda"\n\n[course.periods]\n1 = "09:00-10:30"\n2 = "10:40-12:10"\n\n'
                           '[course.terms]\n"前期" = [4, 5, 6, 7, 8, 9]\n"後期" = [10, 11, 12, 1, 2, 3]\n')
    own = school.from_config(load_config(env={"KEI_AGENT_HOME": str(home)}))
    assert own.at(date(2026, 9, 28), 2)[1].isoformat(timespec="minutes") == "2026-09-28T12:10"
    assert own.span(3) is None                                    # 書いた時限だけ
    assert own.term_of(date(2026, 9, 28)) == "前期" and not own.in_term("後期", date(2026, 9, 28))
    assert own.part is not None                                   # 成績の取り込み方は早稲田のまま


@pytest.mark.parametrize(("values", "message"), [
    ({"school": "nowhere"}, "学校の部品「nowhere」がありません（同梱: waseda"),
    ({"school": "~/nowhere/part.py"}, "学校の部品のファイルがありません"),
    ({"periods": {"1": "9:00"}}, r"\[course\] periods の 1"),
    ({"periods": {"一": "09:00-10:30"}}, r"\[course\] periods の 一"),
    ({"periods": {"1": "10:30-09:00"}}, "終わりを始まりより後"),
    ({"terms": {"前期": [4, 13]}}, r"\[course\] terms の 前期"),
    ({"terms": {"前期": []}}, "1〜12 の数の並び"),
])
def test_a_wrong_school_setting_says_where_to_fix_it(values, message):
    with pytest.raises(school.SchoolError, match=message):
        school.load(values)


def test_an_own_school_part_can_live_in_the_user_folder(tmp_path):
    """同梱に無い学校の部品は、自分のフォルダに書いて場所を指せる（同梱の部品と同じく、記録の形を読める）。"""
    home = _home(tmp_path, '[course]\nschool = "schools/tokyo_tech.py"\n')
    (home / "schools").mkdir()
    (home / "schools" / "tokyo_tech.py").write_text(
        'from ..academic_record import AcademicRecord\n\n'
        'LABEL = "東京科学大学"\nPERIODS = {1: "08:50-10:30"}\nTERMS = {"1Q": [4, 5], "2Q": [6, 7, 8]}\n\n\n'
        'def read_record(files):\n    return AcademicRecord((), ())\n', encoding="utf-8")
    own = school.from_config(load_config(env={"KEI_AGENT_HOME": str(home)}))
    assert (own.name, own.label, own.term_of(date(2026, 6, 1))) == ("tokyo_tech.py", "東京科学大学", "2Q")
    assert own.read_record([]) == AcademicRecord((), ())
    assert own.course_term("1Q") == "1Q" and own.requirement_names("A", "B") == ("B",)   # 書かなければ、そのまま

    (home / "schools" / "broken.py").write_text("raise RuntimeError('書きかけ')\n", encoding="utf-8")
    with pytest.raises(school.SchoolError, match="broken.py を読めません: 書きかけ"):
        school.load({"school": "schools/broken.py"}, home)


def test_waseda_turns_grade_terms_into_course_terms_and_links_grades_with_verified_names():
    waseda = school.load({"school": "waseda"})
    assert waseda.course_term("春期") == "春学期" and waseda.course_term("夏ク") == "夏ク"
    assert waseda.requirement_names("Ａ群", "外国語 英語") == ("外国語 英語", "外国語 英語 必修")
    assert not hasattr(waseda, "gpa_kind") and not hasattr(waseda, "course_group")


def test_course_database_options_come_from_the_school():
    """「授業」の Term の選択肢は学校から。早稲田は今までと同じ名前。"""
    spec = notion_setup.course_spec(school.load({"school": "waseda"}))["properties"]
    assert [option["name"] for option in spec["Term"]["select"]["options"]] == [
        "春学期", "秋学期", "通年", "春ク", "夏ク", "秋ク", "冬ク", "その他"]
    plain = notion_setup.course_spec(school.School())["properties"]
    assert [option["name"] for option in plain["Term"]["select"]["options"]] == ["その他"]
    assert notion_setup.COURSES["properties"]["Term"]["select"]["options"] == []    # 元の形は書き換えない
