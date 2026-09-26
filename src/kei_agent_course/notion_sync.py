"""Moodle の締切を、Notion の「課題」に書き込む。

`kei-agent-course-setup` で作った授業用の Notion に、ics から読んだ締切を1行ずつ入れる。
同じ課題を二重に作らないよう、Moodle のイベント ID（ics の UID）を目印にする。
依頼者が手で直した「状態」「見積時間」「実績時間」には触らない。
取り込むのは「授業」に入れた履修科目の締切だけにする（Moodle のカレンダーには、
新入生向けの資料など、履修していない科目の締切も並ぶため）。

Notion はゲートウェイ経由（client は course）で、授業ホームの中だけに届く。

使い方（手で動かすとき。Notion ゲートウェイが動いていること）:
    source ~/.config/zsh/local/kei-agent.zsh
    uv run --group course kei-agent-course-sync
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError, gateway_notion
from kei_agent_course import periods
from kei_agent_course.course_identity import normalize_course_name
from kei_agent_course.ics import Event
from kei_agent_course.notion_props import number, plain, select

log = logging.getLogger(__name__)

STATE_NAME = "notion-course.json"
NO_STATE = "授業用の Notion がまだありません（kei-agent-course-setup を実行してください）"
# 1回の取り込みで書き込む上限。ics を読み違えたときに、大量の行を作ってしまわないようにする
MAX_WRITES = 50
TITLE_LIMIT = 200
REQUIRED_DATABASES = frozenset({"courses", "assignments", "grades", "requirements", "gpa"})
_QUOTED_DUE = re.compile(r"^「(?P<title>.+)」の提出期限$")
_ASSIGNMENT_SECTIONS = ("やること", "提出物", "進捗メモ", "資料・リンク")


class SyncError(RuntimeError):
    pass


def assignment_title(summary: str) -> str:
    """利用者が読める Moodle 課題名にする。厳密に一致する提出期限だけを短縮する。"""
    match = _QUOTED_DUE.fullmatch(summary)
    return match.group("title") if match else summary


def assignment_template_blocks() -> list[dict]:
    """新規かつ空の課題ページだけに置く、課題ごとの整理見出し。"""
    return [{
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": title}}]},
    } for title in _ASSIGNMENT_SECTIONS]


@dataclass
class Result:
    """取り込みの結果。オーケストレーターがそのまま Slack に出せる形にする。"""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    # 「授業」に無かった科目名
    other_courses: list[str] = field(default_factory=list)
    stopped: bool = False
    # 履修科目だけを取り込んだか（False なら、ほかの科目も科目の欄を空にして入れた）
    known_only: bool = True

    def summary(self) -> str:
        lines = [f"Moodle を見てきたよ（新しい課題 {len(self.added)} 件・締切が変わった課題 "
                 f"{len(self.updated)} 件・そのまま {self.unchanged} 件）"]
        lines += [f"• 新しい: {t}" for t in self.added]
        lines += [f"• 変わった: {t}" for t in self.updated]
        if self.other_courses:
            head = "履修していない科目の締切は入れなかったよ: " if self.known_only else \
                "「授業」に無い科目なので、科目の欄は空にしたよ: "
            lines.append(head + _names(self.other_courses))
        if self.stopped:
            lines.append(f"多すぎたので {MAX_WRITES} 件で止めたよ。もう一度頼むと続きから取り込むね。")
        return "\n".join(lines)


def _names(names: list[str], limit: int = 3, width: int = 24) -> str:
    """科目名を短くつないで出す（Moodle の科目名は長いものがある）。"""
    shown = "、".join(n[:width] for n in names[:limit])
    return shown + (f"（ほか {len(names) - limit} 科目）" if len(names) > limit else "")


def state_path() -> Path:
    return Path(load_config().state_dir) / STATE_NAME


def read_state(path: Path | None = None) -> dict:
    path = path or state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SyncError(NO_STATE) from None
    require_course_databases(state)
    return state


def require_course_databases(state: dict) -> dict[str, dict]:
    """統一済みの授業ホーム state だけを受け入れる。"""
    databases = state.get("databases") or {}
    if not set(databases) >= REQUIRED_DATABASES:
        raise SyncError(NO_STATE)
    return databases


def _when(prop: dict | None) -> datetime | None:
    start = ((prop or {}).get("date") or {}).get("start")
    try:
        return datetime.fromisoformat(start) if start else None
    except ValueError:
        return None


class CourseNotion:
    """授業用の Notion への書き込み（「授業」は読むだけ、「課題」に書く）。"""

    def __init__(self, notion: Notion, state: dict):
        self.notion = notion
        self.courses = state["databases"]["courses"]["data_source_id"]
        self.assignments = state["databases"]["assignments"]["data_source_id"]

    def _rows(self, data_source_id: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{data_source_id}/query", {"page_size": 100})

    def calendar_assignments(self, days: int, today: date) -> dict:
        """課題 DB の締切を全件読む。Moodle ICS の件数上限や同期は通さない。"""
        if not 1 <= days <= 400:
            raise SyncError("取得期間は1〜400日にしてください")
        through = today + timedelta(days=days - 1)
        rows = self.notion.paginate("POST", f"/data_sources/{self.assignments}/query", {
            "filter": {"and": [
                {"property": "締切", "date": {"on_or_after": today.isoformat()}},
                {"property": "締切", "date": {"on_or_before": through.isoformat()}},
            ]},
            "page_size": 100,
        })
        seen = set()
        items = []
        for row in rows:
            page_id = row.get("id")
            if not page_id or page_id in seen:
                raise SyncError("課題 DB のページ ID が欠落または重複しています")
            seen.add(page_id)
            props = row.get("properties") or {}
            due = (props.get("締切", {}).get("date") or {}).get("start")
            if not due:
                continue
            try:
                due_day = date.fromisoformat(due[:10])
            except ValueError:
                raise SyncError(f"課題 {page_id} の締切が不正です") from None
            if not today <= due_day <= through:
                continue
            title = plain(props.get("課題"))
            url = row.get("url")
            if not title or not url:
                raise SyncError(f"課題 {page_id} の名前または URL がありません")
            items.append({"id": page_id, "title": title, "due": due,
                          "status": ((props.get("状態") or {}).get("status") or {}).get("name") or "",
                          "url": url})
        items.sort(key=lambda item: (item["due"], item["title"], item["id"]))
        return {"complete": True, "items": items}

    def course_ids(self) -> dict[str, str]:
        """科目名 → 「授業」のページ ID。"""
        found: dict[str, str] = {}
        for row in self._rows(self.courses):
            if select(row["properties"].get("状態")) == "終了":
                continue
            name = normalize_course_name(plain(row["properties"].get("科目名")))
            if not name:
                continue
            if name in found:
                raise SyncError(f"授業 DB に同じ科目が重複しています: {name}")
            found[name] = row["id"]
        return found

    def courses_on(self, weekday: str = "", on: date | None = None) -> list[dict]:
        """履修中の科目（曜日・時限つき）。weekday を渡すと、その曜日だけ。

        学期の終わった科目が残っていても混ざらないよう、その日の学期（と通年）だけを返す。
        年度が入っている科目は、その日の年度のものだけ（去年の「履修中」を今年に出さない）。
        """
        on = on or date.today()
        found = []
        for row in self._rows(self.courses):
            props = row["properties"]
            if select(props.get("状態")) not in ("", "履修中"):
                continue
            if not periods.in_term(select(props.get("学期")), on, number(props.get("年度"))):
                continue
            day = select(props.get("曜日"))
            if weekday and day != weekday:
                continue
            found.append({
                "id": row["id"],
                "subject": plain(props.get("科目名")),
                "weekday": day,
                "term": select(props.get("学期")),
                "period": (props.get("時限") or {}).get("number"),
                "url": row.get("url", ""),
            })
        found.sort(key=lambda c: (c["period"] is None, c["period"] or 0, c["subject"]))
        return found

    def current_courses(self, on: date | None = None) -> list[dict]:
        """時間カードの候補。曜日で絞らず、今学期に履修中の科目だけ返す。"""
        return self.courses_on(on=on)

    def taken(self) -> dict[str, dict]:
        """Moodle ID → すでにある「課題」の行。"""
        return {uid: row for row in self._rows(self.assignments)
                if (uid := plain(row["properties"].get("Moodle ID")))}

    def ensure_assignment_template(self, page_id: str) -> bool:
        """本文がまだ空の課題ページにだけ、整理用の見出しを一度追加する。"""
        children = self.notion.request("GET", f"/blocks/{page_id}/children").get("results") or []
        if children:
            return False
        self.notion.request("PATCH", f"/blocks/{page_id}/children", {"children": assignment_template_blocks()})
        return True

    def sync(self, events: list[Event], known_only: bool = True) -> Result:
        courses, taken = self.course_ids(), self.taken()
        result = Result(known_only=known_only)
        writes = 0
        for event in events:
            if event.starts_at is None or not event.uid:
                continue
            course_id = courses.get(normalize_course_name(event.course_name))
            if not course_id:
                if event.course_name and event.course_name not in result.other_courses:
                    result.other_courses.append(event.course_name)
                if known_only:
                    continue
            row = taken.get(event.uid)
            if row is not None and not self._differs(row, event, course_id):
                result.unchanged += 1
                continue
            if writes >= MAX_WRITES:
                result.stopped = True
                break
            props = self._properties(event, course_id)
            if row is None:
                props["状態"] = {"status": {"name": "未着手"}}
                page = self.notion.request("POST", "/pages", {
                    "parent": {"type": "data_source_id", "data_source_id": self.assignments},
                    "properties": props})
                self.ensure_assignment_template(page["id"])
                result.added.append(self._label(event))
            else:
                self.notion.request("PATCH", f"/pages/{row['id']}", {"properties": props})
                result.updated.append(self._label(event))
            writes += 1
        log.info("課題を取り込みました: 新規 %d・更新 %d・そのまま %d",
                 len(result.added), len(result.updated), result.unchanged)
        return result

    def _properties(self, event: Event, course_id: str | None) -> dict:
        props = {
            "課題": {"title": [{"text": {"content": assignment_title(event.summary)[:TITLE_LIMIT]}}]},
            # ics は手元の時刻に直してあるので、時差を付けて渡す（Notion 側でずれない）
            "締切": {"date": {"start": event.starts_at.astimezone().isoformat()}},
            "出どころ": {"select": {"name": "Moodle"}},
            "Moodle ID": {"rich_text": [{"text": {"content": event.uid}}]},
        }
        if event.url:
            props["Moodle"] = {"url": event.url}
        if course_id:
            props["科目"] = {"relation": [{"id": course_id}]}
        return props

    def _differs(self, row: dict, event: Event, course_id: str | None) -> bool:
        """Moodle 側と食い違っているか（状態や見積時間は見ない）。"""
        props = row.get("properties") or {}
        if plain(props.get("課題")) != assignment_title(event.summary)[:TITLE_LIMIT]:
            return True
        when = _when(props.get("締切"))
        if when is None or when != event.starts_at.astimezone():
            return True
        if event.url and (props.get("Moodle") or {}).get("url") != event.url:
            return True
        related = [r.get("id") for r in (props.get("科目") or {}).get("relation") or []]
        return bool(course_id) and course_id not in related

    def _label(self, event: Event) -> str:
        head = f"{event.course_name} / " if event.course_name else ""
        return f"{event.starts_at:%m/%d %H:%M} {head}{event.summary}"


def _client(state: dict | None = None) -> CourseNotion:
    """Notion はゲートウェイ経由（client は course。授業ホームの中だけに届く）。状態は省くとファイルから読む。"""
    return CourseNotion(gateway_notion("course"), state or read_state())


def courses_on(weekday: str = "", on: date | None = None, state: dict | None = None) -> list[dict]:
    """履修中の科目（曜日・時限つき）。朝のまとめで、時限を時刻に直すのに使う。"""
    return _client(state).courses_on(weekday, on)


def current_courses(on: date | None = None, state: dict | None = None) -> list[dict]:
    return _client(state).current_courses(on)


def course_names(state: dict | None = None) -> set[str]:
    """「授業」に入れてある科目の名前（Toggl のプロジェクト名と突き合わせるのに使う）。"""
    return set(_client(state).course_ids())


def course_catalog(notion: Notion, state: dict) -> tuple[str, ...]:
    """授業 DB の科目名だけを読み出す。ページ・relation・DB は一切変更しない。"""
    data_source_id = state["databases"]["courses"]["data_source_id"]
    rows = notion.paginate("POST", f"/data_sources/{data_source_id}/query", {"page_size": 100})
    return tuple(sorted({plain(row.get("properties", {}).get("科目名")) for row in rows}
                        - {""}))


def list_calendar_assignments(days: int, today: date | None = None, *, state: dict | None = None) -> dict:
    """A2A 用 read-only 契約。接続できないときに空の完全 snapshot を返さない。"""
    return _client(state).calendar_assignments(days, today or date.today())


def sync(events: list[Event], known_only: bool = True, state: dict | None = None) -> Result:
    """締切を Notion に反映する。"""
    return _client(state).sync(events, known_only=known_only)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    from kei_agent_course import moodle

    parser = argparse.ArgumentParser(prog="kei-agent-course-sync",
                                     description="Moodle の締切を Notion の「課題」に取り込む")
    parser.add_argument("--days", type=int, default=moodle.WINDOW_DAYS, help="何日先まで取り込むか")
    parser.add_argument("--all", action="store_true",
                        help="「授業」に無い科目の締切も入れる（既定は履修科目だけ）")
    args = parser.parse_args(argv)
    url = moodle.ics_url()
    if not url:
        sys.exit(f"{moodle.ICS_ENV} がありません")
    try:
        print(sync(moodle.due(url, days=args.days), known_only=not args.all).summary())
    except (SyncError, NotionError, moodle.MoodleError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
