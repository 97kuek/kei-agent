"""Moodle の登録科目と授業 DB を、変更せずに照合する。"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from kei_agent.notion import Notion, NotionError
from kei_agent_course import moodle, notion_sync
from kei_agent_course.course_identity import normalize_course_name
from kei_agent_course.ics import Event


@dataclass(frozen=True)
class CourseCatalog:
    """ICS と授業 DB の名前だけを並べた読み取り専用の照合結果。"""

    registered: tuple[str, ...]
    known: tuple[str, ...]
    missing: tuple[str, ...]


def compare_course_catalog(events: Sequence[Event], known_names: Iterable[str]) -> CourseCatalog:
    """ICS にある科目名を重複なく集め、既存の授業 DB と比較する。"""
    registered = tuple(sorted({event.course_name for event in events if event.course_name}))
    known_set = {normalize_course_name(name) for name in known_names if name.strip()}
    known = tuple(name for name in registered if normalize_course_name(name) in known_set)
    missing = tuple(name for name in registered if normalize_course_name(name) not in known_set)
    return CourseCatalog(registered, known, missing)


def _section(title: str, names: tuple[str, ...]) -> str:
    return "\n".join([f"{title}（{len(names)}件）", *(f"• {name}" for name in names)])


def main(argv: list[str] | None = None) -> None:
    """読み取り専用の Moodle 科目照合を実行する。"""
    del argv  # CLI の引数は持たず、意図しない同期オプションを作らない
    url = moodle.ics_url()
    if not url:
        sys.exit(f"{moodle.ICS_ENV} がありません")
    token = notion_sync.course_token()
    if not token:
        sys.exit(notion_sync.NO_TOKEN)
    try:
        report = compare_course_catalog(
            moodle.events(url),
            notion_sync.course_catalog(Notion(token), notion_sync.read_state()),
        )
    except (notion_sync.SyncError, NotionError, moodle.MoodleError) as error:
        sys.exit(str(error))
    print(_section("Moodle 登録科目", report.registered))
    print(_section("授業DB にある科目", report.known))
    print(_section("確認が必要な科目", report.missing))
