"""授業ホームの学業記録 DB を安全に点検する。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from kei_agent_course.notion_setup import SPECS


@dataclass(frozen=True)
class MigrationReport:
    """実データを変更せずに返す DB 整理の確認結果。"""

    missing: tuple[str, ...]
    duplicates: tuple[str, ...]
    unmanaged: tuple[str, ...]
    ambiguous_courses: tuple[str, ...] = ()


def inspect_course_home(notion, home_page_id: str, state: dict) -> MigrationReport:
    """授業ホーム直下の DB を読むだけで点検する。"""
    del state  # state の既存 ID は migration 実行時に使う。dry-run では child DB を正とする。
    titles = [
        str(block.get("child_database", {}).get("title") or "")
        for block in notion.children(home_page_id)
        if block.get("type") == "child_database"
    ]
    canonical = {title for title, _spec in SPECS.values()}
    counts = Counter(titles)
    return MigrationReport(
        missing=tuple(sorted(canonical - set(titles))),
        duplicates=tuple(sorted(title for title, count in counts.items() if title in canonical and count > 1)),
        unmanaged=tuple(sorted(title for title in titles if title and title not in canonical)),
    )
