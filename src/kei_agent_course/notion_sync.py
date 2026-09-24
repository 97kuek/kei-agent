"""Moodle の締切を、Notion の「課題」に書き込む。

`kei-agent-course-setup` で作った授業用の Notion に、ics から読んだ締切を1行ずつ入れる。
同じ課題を二重に作らないよう、Moodle のイベント ID（ics の UID）を目印にする。
依頼者が手で直した「状態」「見積時間」「実績時間」には触らない。
取り込むのは「授業」に入れた履修科目の締切だけにする（Moodle のカレンダーには、
新入生向けの資料など、履修していない科目の締切も並ぶため）。

使い方（手で動かすとき）:
    source ~/.config/zsh/local/kei-agent.zsh
    uv run --group course kei-agent-course-sync
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError
from kei_agent_course import periods
from kei_agent_course.course_identity import normalize_course_name
from kei_agent_course.ics import Event

log = logging.getLogger(__name__)

TOKEN_ENV = "NOTION_COURSE_TOKEN"
STATE_NAME = "notion-course.json"
NO_TOKEN = f"{TOKEN_ENV} がありません（授業用のコネクトのトークンを、秘密情報のファイルに入れてください）"
NO_STATE = "授業用の Notion がまだありません（kei-agent-course-setup <授業ホームのページID> を実行してください）"
# 1回の取り込みで書き込む上限。ics を読み違えたときに、大量の行を作ってしまわないようにする
MAX_WRITES = 50
TITLE_LIMIT = 200
REQUIRED_DATABASES = frozenset({"courses", "assignments", "study_logs", "grades", "requirements", "gpa"})
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


def _plain(prop: dict | None) -> str:
    """title と rich_text の中身を、ただの文字列にする。"""
    parts = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _select(prop: dict | None) -> str:
    return ((prop or {}).get("select") or {}).get("name") or ""


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
        self.study_logs = (state["databases"].get("study_logs") or {}).get("data_source_id", "")
        self._recorded_study_ids: set[str] = set()

    def _rows(self, data_source_id: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{data_source_id}/query", {"page_size": 100})

    def course_ids(self) -> dict[str, str]:
        """科目名 → 「授業」のページ ID。"""
        found: dict[str, str] = {}
        for row in self._rows(self.courses):
            if _select(row["properties"].get("状態")) == "終了":
                continue
            name = normalize_course_name(_plain(row["properties"].get("科目名")))
            if not name:
                continue
            if name in found:
                raise SyncError(f"授業 DB に同じ科目が重複しています: {name}")
            found[name] = row["id"]
        return found

    def courses_on(self, weekday: str = "", on: date | None = None) -> list[dict]:
        """履修中の科目（曜日・時限つき）。weekday を渡すと、その曜日だけ。

        学期の終わった科目が残っていても混ざらないよう、その日の学期（と通年）だけを返す。
        """
        on = on or date.today()
        found = []
        for row in self._rows(self.courses):
            props = row["properties"]
            if _select(props.get("状態")) not in ("", "履修中"):
                continue
            if not periods.in_term(_select(props.get("学期")), on):
                continue
            day = _select(props.get("曜日"))
            if weekday and day != weekday:
                continue
            found.append({
                "id": row["id"],
                "subject": _plain(props.get("科目名")),
                "weekday": day,
                "term": _select(props.get("学期")),
                "period": (props.get("時限") or {}).get("number"),
                "url": row.get("url", ""),
            })
        found.sort(key=lambda c: (c["period"] is None, c["period"] or 0, c["subject"]))
        return found

    def current_courses(self, on: date | None = None) -> list[dict]:
        """時間カードの候補。曜日で絞らず、今学期に履修中の科目だけ返す。"""
        return self.courses_on(on=on)

    def match_course(self, name: str, year: int, term: str) -> str | None:
        """成績 record を過去を含む科目台帳へ安全に結ぶ。

        同名の科目が複数ある場合や、年度・学期が記録されていない場合は推測しない。
        """
        normalized_term = {"春期": "春学期", "秋期": "秋学期"}.get(term, term)
        matches = [row["id"] for row in self._rows(self.courses) if (
            _plain(row.get("properties", {}).get("科目名")) == name
            and (row.get("properties", {}).get("年度") or {}).get("number") == year
            and _select(row.get("properties", {}).get("学期")) == normalized_term
        )]
        return matches[0] if len(matches) == 1 else None

    def record_study_time(self, entry_id: str, started_at: str, duration_minutes: int,
                          course_page_id: str = "", memo: str = "", slack_url: str = "") -> dict:
        """学習ログを記録IDで一度だけ作る。"""
        if not self.study_logs:
            raise SyncError("「学習ログ」がありません。kei-agent-course-setup をもう一度実行してください")
        if entry_id in self._recorded_study_ids:
            return {"entry_id": entry_id, "notion_url": ""}
        for row in self._rows(self.study_logs):
            if _plain(row.get("properties", {}).get("Kei Agent 記録ID")) == entry_id:
                self._recorded_study_ids.add(entry_id)
                return {"entry_id": entry_id, "notion_url": row.get("url", "")}
        title = "大学の学習"
        if course_page_id:
            for row in self._rows(self.courses):
                if row.get("id") == course_page_id:
                    title = _plain(row.get("properties", {}).get("科目名")) or title
                    break
        props = {
            "タイトル": {"title": [{"text": {"content": title}}]},
            "Kei Agent 記録ID": {"rich_text": [{"text": {"content": entry_id}}]},
            "日付": {"date": {"start": started_at}},
            "時間（分）": {"number": duration_minutes},
            "メモ": {"rich_text": [{"text": {"content": memo}}]} if memo else {"rich_text": []},
            "Slack": {"url": slack_url} if slack_url else {"url": None},
        }
        if course_page_id:
            props["科目"] = {"relation": [{"id": course_page_id}]}
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self.study_logs}, "properties": props})
        self._recorded_study_ids.add(entry_id)
        return {"entry_id": entry_id, "notion_url": page.get("url", "")}

    def taken(self) -> dict[str, dict]:
        """Moodle ID → すでにある「課題」の行。"""
        return {uid: row for row in self._rows(self.assignments)
                if (uid := _plain(row["properties"].get("Moodle ID")))}

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
        if _plain(props.get("課題")) != assignment_title(event.summary)[:TITLE_LIMIT]:
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


def courses_on(weekday: str = "", on: date | None = None, token: str = "",
               state: dict | None = None) -> list[dict]:
    """履修中の科目（曜日・時限つき）。朝のまとめで、時限を時刻に直すのに使う。"""
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SyncError(NO_TOKEN)
    return CourseNotion(Notion(token), state or read_state()).courses_on(weekday, on)


def current_courses(on: date | None = None, token: str = "", state: dict | None = None) -> list[dict]:
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SyncError(NO_TOKEN)
    return CourseNotion(Notion(token), state or read_state()).current_courses(on)


def record_study_time(entry_id: str, started_at: str, duration_minutes: int, course_page_id: str = "",
                      memo: str = "", slack_url: str = "", token: str = "", state: dict | None = None) -> dict:
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SyncError(NO_TOKEN)
    current = state or read_state()
    if "study_logs" not in current.get("databases", {}):
        from kei_agent_course.notion_setup import CourseSetup

        setup = CourseSetup(Notion(token), str(current.get("home_page_id") or ""), state_path())
        if not setup.home:
            raise SyncError(NO_STATE)
        setup.run()
        current = setup.state
    return CourseNotion(Notion(token), current).record_study_time(
        entry_id, started_at, duration_minutes, course_page_id, memo, slack_url)


def course_names(token: str = "", state: dict | None = None) -> set[str]:
    """「授業」に入れてある科目の名前（Toggl のプロジェクト名と突き合わせるのに使う）。"""
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SyncError(NO_TOKEN)
    return set(CourseNotion(Notion(token), state or read_state()).course_ids())


def course_token(env: dict[str, str] | None = None) -> str:
    """授業ホーム用トークンを読む。値は返すだけで表示しない。"""
    return (dict(os.environ) if env is None else env).get(TOKEN_ENV, "")


def course_catalog(notion: Notion, state: dict) -> tuple[str, ...]:
    """授業 DB の科目名だけを読み出す。ページ・relation・DB は一切変更しない。"""
    data_source_id = state["databases"]["courses"]["data_source_id"]
    rows = notion.paginate("POST", f"/data_sources/{data_source_id}/query", {"page_size": 100})
    return tuple(sorted({_plain(row.get("properties", {}).get("科目名")) for row in rows}
                        - {""}))


def sync(events: list[Event], known_only: bool = True, token: str = "", state: dict | None = None) -> Result:
    """締切を Notion に反映する。トークンと状態は、省くと環境変数とファイルから読む。"""
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SyncError(NO_TOKEN)
    return CourseNotion(Notion(token), state or read_state()).sync(events, known_only=known_only)


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
