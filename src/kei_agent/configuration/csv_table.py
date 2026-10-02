"""利用者が手で書く表（agents.csv・schedules.csv）の読み方で、どの表にも共通のもの。

- UTF-8 で読む（Excel が付ける BOM は外す）。読めなければ、保存し直すよう知らせる
- 1行目は列の名前。足りない列・知らない列・同じ名前の列があれば断る
- 1列目が空か `#` で始まる行は飛ばす（メモに使える）。どのセルも前後の空白を外す
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from pathlib import Path

_BOOL = {"true": True, "false": False}


class TableError(ValueError):
    pass


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise TableError(f"{path.name} を UTF-8 として読めません。UTF-8（CSV UTF-8）で保存し直してください") from None


def parse_bool(value: str, where: str, column: str = "enabled") -> bool:
    found = _BOOL.get(value.strip().lower())
    if found is None:
        raise TableError(f"{where} の {column} は true か false にしてください: {value!r}")
    return found


def rows(text: str, name: str, columns: tuple[str, ...], optional: tuple[str, ...] = (),
         too_many: str = "") -> Iterator[tuple[int, dict[str, str]]]:
    """表の行（行番号、列の名前 → 値）。1列目が空か `#` で始まる行は飛ばす。too_many は列が多すぎるときに足す一言。"""
    reader = csv.DictReader(io.StringIO(text))
    header = [column.strip() for column in reader.fieldnames or ()]
    if not set(columns) - set(optional) <= set(header) <= set(columns) or len(set(header)) != len(header):
        raise TableError(f"{name} の1行目（列の名前）は {','.join(columns)} にしてください（今は {','.join(header) or '空'}）")
    reader.fieldnames = header
    first = columns[0]
    for line, raw in enumerate(reader, start=2):
        head = (raw.get(first) or "").strip()
        if not head or head.startswith("#"):
            continue    # メモの行は、列の数を数えない
        if None in raw:
            raise TableError(f"{name} の {line} 行目は列が多すぎます{too_many}")
        yield line, {key: (value or "").strip() for key, value in raw.items() if key is not None}
