"""Moodle と授業 DB で同じ科目を指す表記をそろえる。"""

from __future__ import annotations

import unicodedata


def normalize_course_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip()
