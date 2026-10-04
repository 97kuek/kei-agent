"""学校ごとの違い（docs/agents/course-agent.md）。時限の時刻・学期と、成績の取り込み方。

時限の時刻と学期は設定（config.toml の [course] の periods・terms）で決まり、書かなければ学校の部品の既定を使う。
成績の取り込み方は学校の部品が持つ。部品は [course] school で選ぶ。同梱の部品は schools/<名前>.py（早稲田は
"waseda"）で、自分で書いた部品は、そのファイルの場所（~ から、または利用者のフォルダからの相対）を書く。

学校の部品に置けるもの（どれも任意。見本は schools/waseda.py）:

- `LABEL` … 学校の名前
- `PERIODS` … 時限 → "HH:MM-HH:MM"（設定の periods と同じ書き方）
- `TERMS` … 学期 → 開かれる月の並び（設定の terms と同じ書き方）。最初の学期の最初の月を、年度の始まりにする。
  授業 DB の「学期」に入れる名前は、クォーターなども全部書く（含まれる学期と同じ月にすれば、その学期のあいだ履修中）
- `read_record(files)` … 成績のファイル（`kei-agent-module course academic-import` に渡したもの）を読んで、
  AcademicRecord（academic_record.py）を返す。無ければ成績は取り込めない
- `course_term(term)` … 成績の学期 → 授業 DB の学期（無ければ、そのまま）
- `requirement_names(group, category)` … 成績の区分に当たる「単位要件」の名前の候補（無ければ区分の名前だけ）
- 単位要件の kind は academic_record.py の KINDS（Category / Subtotal / Total / Other）のどれかで返す
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

from kei_agent_a2a.api import WEEKDAYS, Config, settings

from .academic_record import AcademicRecord

MODULE = "course"
# 同梱の学校の部品の置き場所
PARTS_DIR = Path(__file__).resolve().parent / "schools"
# 学期を書かなかったときの、年度の始まりの月（日本の学校の年度）
YEAR_START_MONTH = 4
# 学期が分からない成績や科目（授業 DB の選択肢にも足す）
OTHER = "その他"
_SPAN = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_PART_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class SchoolError(ValueError):
    """学校の設定（時限・学期）や、学校の部品が読めない。"""


@dataclass(frozen=True)
class School:
    """その学校の時限の時刻・学期と、学校の部品（成績の取り込み方）。何も選ばなければ、時限も学期も分からない学校。"""
    name: str = ""
    label: str = ""
    periods: dict[int, tuple[time, time]] = field(default_factory=dict)
    terms: dict[str, tuple[int, ...]] = field(default_factory=dict)
    part: ModuleType | None = None

    # 学期と年度

    @property
    def year_start(self) -> int:
        """年度の始まりの月（最初の学期の最初の月）。"""
        first = next(iter(self.terms.values()), ())
        return first[0] if first else YEAR_START_MONTH

    def academic_year(self, day: date) -> int:
        """その日が属する年度。"""
        return day.year if day.month >= self.year_start else day.year - 1

    def term_of(self, day: date) -> str:
        """その日の学期（書いた順で、その月を含む最初の学期）。分からなければ空文字。"""
        return next((name for name, months in self.terms.items() if day.month in months), "")

    def in_term(self, term: str, day: date, year: int | None = None) -> bool:
        """その学期の科目が、その日に開かれているか。

        年度が入っていれば、その日の年度と合うものだけ。学期が空欄や、知らない名前（「その他」など）のものは
        判断せず対象に含める。
        """
        if year is not None and year != self.academic_year(day):
            return False
        months = self.terms.get(term)
        return months is None or day.month in months

    # 時限

    def span(self, period) -> tuple[time, time] | None:
        """時限の始まりと終わり。時限が無い（集中講義など）か、時刻の分からない時限なら None。"""
        try:
            return self.periods[int(period)]
        except (TypeError, ValueError, KeyError):
            return None

    def at(self, day: date, period) -> tuple[datetime, datetime] | None:
        """その日の、その時限の始まりと終わり。"""
        found = self.span(period)
        if found is None:
            return None
        return datetime.combine(day, found[0]), datetime.combine(day, found[1])

    # 成績の取り込み方（学校の部品）

    def _part(self, name: str):
        return getattr(self.part, name, None) if self.part is not None else None

    def read_record(self, files: list[Path]) -> AcademicRecord:
        """成績のファイルを、学校の部品で読む。部品に読み方が無ければ SchoolError。"""
        read = self._part("read_record")
        if read is None:
            raise SchoolError("学校の部品が選ばれていません。config.toml の [course] school に書いてください（早稲田は \"waseda\"）"
                              if self.part is None else f"学校の部品「{self.name}」には、成績の取り込み方がありません")
        record = read(list(files))
        if not isinstance(record, AcademicRecord):
            raise SchoolError(f"学校の部品「{self.name}」の read_record が、AcademicRecord を返しませんでした")
        return record

    def course_term(self, term: str) -> str:
        """成績の学期 → 授業 DB の学期。"""
        convert = self._part("course_term")
        return convert(term) if convert is not None else term

    def requirement_names(self, group: str, category: str) -> tuple[str, ...]:
        """成績の区分に当たる単位要件の名前の候補（前から順に探す）。"""
        names = self._part("requirement_names")
        return tuple(names(group, category)) if names is not None else (category,)


def next_weekday(weekday: str, today: date) -> date:
    """今日から見て、次のその曜日の日付（今日がその曜日なら今日）。曜日でなければ今日。"""
    index = WEEKDAYS.find(weekday) if len(weekday) == 1 else -1
    if index < 0:
        return today
    return today + timedelta(days=(index - today.weekday()) % 7)


def bundled() -> tuple[str, ...]:
    """同梱の学校の部品の名前。"""
    return tuple(sorted(path.stem for path in PARTS_DIR.glob("*.py") if _PART_NAME.match(path.stem)))


def _load_part(name: str, user_dir: Path | None) -> ModuleType | None:
    """学校の部品を読む。名前なら同梱のもの、それ以外は自分で書いた部品のファイルの場所。"""
    if not name:
        return None
    package = f"{__package__}.schools"
    importlib.import_module(package)
    if _PART_NAME.match(name):
        if name not in bundled():
            raise SchoolError(f"config.toml の [course] school: 学校の部品「{name}」がありません（同梱: "
                              f"{', '.join(bundled()) or 'なし'}。自分で書いた部品なら、ファイルの場所を書いてください）")
        return importlib.import_module(f"{package}.{name}")
    path = Path(name).expanduser()
    if not path.is_absolute() and user_dir is not None:
        path = user_dir / path
    if path.suffix != ".py" or not path.is_file():
        raise SchoolError(f"config.toml の [course] school: 学校の部品のファイルがありません: {path}")
    # 同梱の部品と同じ場所に並べる（部品の中から `from ..academic_record import ...` で読めるように）
    key = f"{package}.own_{re.sub(r'\W', '_', path.stem)}"
    loaded = sys.modules.get(key)
    if loaded is not None and getattr(loaded, "__file__", None) == str(path):
        return loaded
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise SchoolError(f"学校の部品を読めません: {path}")
    part = importlib.util.module_from_spec(spec)
    sys.modules[key] = part
    try:
        spec.loader.exec_module(part)
    except Exception as e:  # 利用者の書いた部品。何が起きても、どのファイルかを添えて知らせる
        del sys.modules[key]
        raise SchoolError(f"学校の部品 {path} を読めません: {e}") from None
    return part


def _span(value: object) -> tuple[time, time] | None:
    match = _SPAN.match(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        start, end = time(int(match[1]), int(match[2])), time(int(match[3]), int(match[4]))
    except ValueError:
        return None
    return (start, end) if start < end else None


def _periods(raw: object, where: str) -> dict[int, tuple[time, time]]:
    """時限 → (始まり, 終わり)。書き方が違えば SchoolError。"""
    if not isinstance(raw, dict):
        raise SchoolError(f"{where} は「時限 = \"HH:MM-HH:MM\"」の表にしてください（例: 1 = \"08:50-10:30\"）")
    found = {}
    for key, value in raw.items():
        span = _span(value)
        if not str(key).isdigit() or span is None:
            raise SchoolError(f"{where} の {key} = {value!r}: 「時限 = \"HH:MM-HH:MM\"」の形で、終わりを始まりより後にしてください")
        found[int(key)] = span
    return dict(sorted(found.items()))


def _terms(raw: object, where: str) -> dict[str, tuple[int, ...]]:
    """学期 → 開かれる月。書き方が違えば SchoolError。"""
    if not isinstance(raw, dict):
        raise SchoolError(f"{where} は「学期 = [開かれる月, ...]」の表にしてください（例: \"春学期\" = [4, 5, 6, 7, 8]）")
    found = {}
    for name, months in raw.items():
        if (not str(name).strip() or not isinstance(months, (list, tuple)) or not months
                or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12 for m in months)):
            raise SchoolError(f"{where} の {name}: 開かれる月を、1〜12 の数の並びで書いてください")
        found[str(name)] = tuple(months)
    return found


def load(values: dict, user_dir: Path | None = None) -> School:
    """設定（[course] の school・periods・terms）から学校を作る。書き方が違えば SchoolError。"""
    name = str(values.get("school") or "").strip()
    part = _load_part(name, user_dir)
    shown = name if _PART_NAME.match(name) else Path(name).name
    periods, terms = values.get("periods"), values.get("terms")
    return School(
        name=shown,
        label=str(getattr(part, "LABEL", "") or ""),
        periods=(_periods(periods, "config.toml の [course] periods") if periods
                 else _periods(getattr(part, "PERIODS", None) or {}, f"学校の部品「{shown}」の PERIODS")),
        terms=(_terms(terms, "config.toml の [course] terms") if terms
               else _terms(getattr(part, "TERMS", None) or {}, f"学校の部品「{shown}」の TERMS")),
        part=part,
    )


def from_config(config: Config) -> School:
    """設定から、その学校を読む（大学の担当と、手で動かすコマンドが使う）。"""
    return load(settings(config, MODULE), config.user_dir)
