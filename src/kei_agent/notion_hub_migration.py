"""共通ホームへの一回限りの移行。どれも既定は確認だけで、書くには --apply が要る。

- 引数なし: 研究 Notes の Daily／振り返りを監査し、`--apply --expected-count N` で日別記録へ移す
- `--local`: 手元の `overview/daily/<日付>.md`・`overview/reviews/<日付>.md` を日別記録へ写す（ファイルは消さない）
- `--time`: 研究ホームの「研究ログ」、授業ホームの「学習ログ」、手元の仕事の記録を時間記録へ写す（旧 DB は消さない）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError, gateway_notion, write_json_atomic
from kei_agent.notion_hub import MANAGED_END, HubStore, load_hub
from kei_agent.notion_store import NotionStore, blocks_to_markdown, plain_text
from kei_agent.store import Store


@dataclass(frozen=True)
class MigrationEntry:
    page_id: str
    url: str
    body: str
    day: str
    kind: str
    title: str
    slack_url: str | None
    checksum: str
    properties: dict


@dataclass(frozen=True)
class MigrationManifest:
    notes_ds_id: str
    entries: tuple[MigrationEntry, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MigrationManifest:
        return cls(data["notes_ds_id"], tuple(MigrationEntry(**item) for item in data["entries"]))


@dataclass(frozen=True)
class MigrationReport:
    copied: int
    moved: int
    unresolved: tuple[str, ...]


def _checksum(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _legacy_body(notion: Notion, page_id: str) -> str:
    blocks = notion.children(page_id)
    supported = {"heading_1", "heading_2", "heading_3", "paragraph", "bulleted_list_item",
                 "numbered_list_item", "to_do", "quote", "code", "divider"}
    rendered_blocks = []
    for block in blocks:
        kind = block.get("type")
        if kind not in supported or block.get("has_children"):
            raise NotionError(f"旧記録 {page_id} に未対応の block {kind} があります。原本を維持して停止します")
        rich = []
        for item in block.get(kind, {}).get("rich_text") or []:
            annotations = item.get("annotations") or {}
            if item.get("type") != "text" or item.get("href") or item.get("text", {}).get("link") or any(
                value for key, value in annotations.items() if key not in {"color", "bold", "code"} and value
            ):
                raise NotionError(f"旧記録 {page_id} に書式・リンク付き本文があります。原本を維持して停止します")
            if annotations.get("bold") and annotations.get("code"):
                raise NotionError(f"旧記録 {page_id} に複合書式があります。原本を維持して停止します")
            text = item.get("plain_text") or item.get("text", {}).get("content", "")
            if annotations.get("bold"):
                text = f"**{text}**"
            elif annotations.get("code"):
                text = f"`{text}`"
            rich.append({"plain_text": text})
        rendered_blocks.append({**block, kind: {**block.get(kind, {}), "rich_text": rich}})
    return blocks_to_markdown(rendered_blocks)


def audit_legacy_notes(notion: Notion, notes_ds_id: str) -> MigrationManifest:
    """全件を取得し、曖昧な元ページが1件でもあれば書込前に止める。"""
    rows = notion.paginate("POST", f"/data_sources/{notes_ds_id}/query", {
        "filter": {"or": [
            {"property": "種類", "select": {"equals": "Daily"}},
            {"property": "種類", "select": {"equals": "振り返り"}},
        ]},
        "page_size": 100,
    })
    entries = []
    seen = set()
    for row in rows:
        page_id = row["id"]
        if page_id in seen:
            raise NotionError(f"旧記録 {page_id} が取得結果で重複しています")
        seen.add(page_id)
        props = row.get("properties") or {}
        kind = (props.get("種類", {}).get("select") or {}).get("name")
        day = (props.get("日付", {}).get("date") or {}).get("start")
        if kind not in {"Daily", "振り返り"} or not day:
            raise NotionError(f"旧記録 {page_id} の種類または日付がありません")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise NotionError(f"旧記録 {page_id} の日付が不正です") from None
        title = plain_text(props.get("タイトル", {}).get("title") or [])
        url = row.get("url")
        if not title or not url:
            raise NotionError(f"旧記録 {page_id} のタイトルまたは URL がありません")
        try:
            body = _legacy_body(notion, page_id)
        except (NotionError, KeyError) as e:
            raise NotionError(f"旧記録 {page_id} の本文を読めません: {e}") from None
        if not body.strip():
            raise NotionError(f"旧記録 {page_id} の本文が空です")
        entries.append(MigrationEntry(
            page_id, url, body, day, kind, title,
            props.get("Slack", {}).get("url"),
            _checksum(body), props,
        ))
    return MigrationManifest(notes_ds_id, tuple(entries))


ARCHIVE_TITLE = "旧 Daily・レトプラ記録"


def _find_archive(notion: Notion, home_id: str) -> str | None:
    """旧記録置き場の ID。重複していたら止める。"""
    matches = [block["id"] for block in notion.children(home_id)
               if block.get("type") == "child_page"
               and block.get("child_page", {}).get("title") == ARCHIVE_TITLE]
    if len(matches) > 1:
        raise NotionError("旧記録置き場が重複しています")
    return matches[0] if matches else None


def verify_manifest_current(notion: Notion, manifest: MigrationManifest, home_id: str) -> None:
    """部分移動後も manifest の全原本を ID で検証し、新規旧行は拒否する。"""
    archive_id = _find_archive(notion, home_id)
    expected = {entry.page_id for entry in manifest.entries}
    if len(expected) != len(manifest.entries):
        raise NotionError("manifest に元 ID の重複があります")
    for entry in manifest.entries:
        page = _original_unchanged(notion, entry)
        parent = page.get("parent") or {}
        if not (parent.get("data_source_id") == manifest.notes_ds_id
                or archive_id and parent.get("page_id") == archive_id):
            raise NotionError(f"旧記録 {entry.page_id} が想定外の場所にあります")
    remaining = audit_legacy_notes(notion, manifest.notes_ds_id)
    if any(entry.page_id not in expected for entry in remaining.entries):
        raise NotionError("監査後に移行元の記録が増えました。dry-run からやり直してください")


def _entry_text(entry: MigrationEntry) -> str:
    return (f"[kei-agent:legacy:{entry.page_id}:start]\n"
            f"### {entry.title}\n元ページ: {entry.url}\n"
            f"{entry.body}\n[kei-agent:legacy:{entry.page_id}:end]")


def _copied(section: str, entry: MigrationEntry) -> bool:
    start = f"[kei-agent:legacy:{entry.page_id}:start]"
    end = f"[kei-agent:legacy:{entry.page_id}:end]"
    if section.count(start) != 1 or section.count(end) != 1:
        return False
    chunk = section.split(start, 1)[1].split(end, 1)[0]
    expected = _entry_text(entry).split(start, 1)[1].split(end, 1)[0]
    return _visible_text(chunk) == _visible_text(expected)


def _visible_text(value: str) -> str:
    # 日別記録では見出し1・2を3にそろえて入れるので、見出しの深さは比べない
    return "\n".join(re.sub(r"^#{1,6}\s+", "", line.strip())
                     for line in re.sub(r"\*\*|`", "", value).splitlines() if line.strip())


def _original_unchanged(notion: Notion, entry: MigrationEntry) -> dict:
    page = notion.request("GET", f"/pages/{entry.page_id}")
    body = _legacy_body(notion, entry.page_id)
    if page.get("url") != entry.url or _checksum(body) != entry.checksum:
        raise NotionError(f"旧記録 {entry.page_id} の URL または本文が監査後に変わりました")
    return page


def _archive_parent(notion: Notion, home_id: str, found: str | None) -> str:
    if found:
        return found
    page = notion.request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": home_id},
        "properties": {"title": {"title": [{"text": {"content": ARCHIVE_TITLE}}]}},
    })
    return page["id"]


def _archive_research_view(notion: Notion, research_home_id: str) -> None:
    """対象 block を一意に同定できるときだけ旧表示を外す。ノート DB 自体は残す。"""
    blocks = notion.children(research_home_id)
    title = "最近の Daily と振り返り"
    headings = [b for b in blocks if b.get("type") == "heading_2"
                and plain_text(b["heading_2"].get("rich_text", [])) == title]
    linked = [b for b in blocks if b.get("type") == "child_database"
              and b.get("child_database", {}).get("title") == title]
    if not headings and not linked:
        return
    if len(headings) != 1 or len(linked) != 1:
        raise NotionError("研究ホームの旧 Daily／振り返りビューを一意に同定できません")
    for block in (linked[0], headings[0]):
        notion.request("PATCH", f"/blocks/{block['id']}", {"in_trash": True})


def apply_legacy_notes(notion: Notion, hub: HubStore, manifest: MigrationManifest,
                       store: Store, research_home_id: str) -> MigrationReport:
    """原本照合→コピー照合→移動照合→SQLite 切替。失敗時は原本を削除しない。"""
    if manifest.notes_ds_id == hub.state.daily_ds_id:
        raise NotionError("移行元と移行先が同じ data source です")
    pages = {entry.page_id: _original_unchanged(notion, entry) for entry in manifest.entries}
    if len(pages) != len(manifest.entries):
        raise NotionError("manifest に元 ID の重複があります")
    # archive が既にある場合の重複も、コピー開始前に検出する。
    found_archive = _find_archive(notion, hub.state.home_id)
    groups: dict[tuple[str, str], list[MigrationEntry]] = defaultdict(list)
    for entry in manifest.entries:
        groups[(entry.day, entry.kind)].append(entry)
    mapping = {}
    copied = 0
    for (day, kind), entries in groups.items():
        existing = hub.section_body(day, kind)
        missing = [entry for entry in entries if not _copied(existing, entry)]
        if missing:
            text = "\n\n".join(filter(None, [existing, *(_entry_text(entry) for entry in missing)]))
            latest = missing[-1]
            hub.upsert_day(kind, day, latest.title, text, latest.slack_url)
            copied += len(missing)
        section = hub.section_body(day, kind)
        if any(not _copied(section, entry) for entry in entries):
            raise NotionError(f"{day} {kind} のコピー本文・URL を照合できません")
        row = hub._day(day)
        if row is None:
            raise NotionError(f"{day} の日別行を再取得できません")
        hub.set_legacy_ids(day, [entry.page_id for entry in entries])
        for entry in entries:
            mapping[entry.page_id] = row["id"]
    archive_id = _archive_parent(notion, hub.state.home_id, found_archive)
    moved = 0
    unresolved = []
    for entry in manifest.entries:
        page = pages[entry.page_id]
        if page.get("parent", {}).get("page_id") != archive_id:
            try:
                notion.request("POST", f"/pages/{entry.page_id}/move", {
                    "parent": {"type": "page_id", "page_id": archive_id}})
            except NotionError:
                unresolved.append(entry.page_id)
                continue
            moved += 1
        try:
            after = _original_unchanged(notion, entry)
            if after.get("parent", {}).get("page_id") != archive_id:
                unresolved.append(entry.page_id)
        except NotionError:
            unresolved.append(entry.page_id)
    if unresolved:
        return MigrationReport(copied, moved, tuple(unresolved))
    store.relink_notion_pages(mapping)
    _archive_research_view(notion, research_home_id)
    return MigrationReport(copied, moved, ())


def _save_manifest(path: Path, manifest: MigrationManifest) -> None:
    write_json_atomic(path, manifest.to_dict())


# 手元の daily/・reviews/（--local）

LOCAL_DIRS = (("daily", "Daily"), ("reviews", "振り返り"))


@dataclass(frozen=True)
class LocalCopy:
    path: str
    # copied: 空の区画へ写す / appended: 日別記録に別の本文があるので区画の末尾へ足す
    # present: 同じ文がもうある / problem: 写せない
    status: str
    detail: str = ""


def _visible_lines(text: str) -> list[str]:
    """見た目の文字だけを1行ずつ。装飾、行頭の印、見出しの深さ、空白、空行の違いは比べない。"""
    found = []
    for line in text.splitlines():
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line)
        line = " ".join(re.sub(r"[*_`~]", "", line).split())
        if line and line != MANAGED_END:
            found.append(line)
    return found


def copy_local_notes(hub: HubStore, overview_dir: Path, apply: bool) -> list[LocalCopy]:
    """`daily/<日付>.md` と `reviews/<日付>.md` を、その日の日別記録（Daily／レトプラ）に写す。

    同じ文がもうあれば何もしない（何度実行してもよい）。日別記録に別の本文があるときは消さず、
    区画の末尾に出どころ付きで足す。apply=False なら書かずに、どうなるかだけを返す。ファイルは消さない。
    """
    report = []
    for folder, kind in LOCAL_DIRS:
        for path in sorted((overview_dir / folder).glob("*.md")):
            rel = f"{folder}/{path.name}"
            try:
                day = date.fromisoformat(path.stem).isoformat()
            except ValueError:
                report.append(LocalCopy(rel, "problem", "ファイル名が日付ではありません"))
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
                if not text:
                    report.append(LocalCopy(rel, "problem", "空のファイルです"))
                    continue
                want = _visible_lines(text)
                have = set(_visible_lines(hub.section_text(day, kind)))
                missing = [line for line in want if line not in have]
                if not missing:
                    report.append(LocalCopy(rel, "present"))
                elif hub.section_body(day, kind).strip():
                    if apply:
                        hub.append_to_section(day, kind, f"### ローカルのファイルから（{rel}）\n{text}")
                    report.append(LocalCopy(rel, "appended", f"日別記録に無い行 {len(missing)}/{len(want)}"))
                else:
                    if apply:
                        hub.upsert_day(kind, day, f"{kind} {day}", text, None)
                    report.append(LocalCopy(rel, "copied"))
            except (NotionError, OSError, ValueError) as e:
                report.append(LocalCopy(rel, "problem", str(e)))
    return report


# 旧研究ログ・旧学習ログ・手元の仕事の記録（--time）

@dataclass(frozen=True)
class TimeRow:
    entry_id: str
    domain: str
    label: str
    started_at: str
    minutes: int
    memo: str = ""
    slack_url: str = ""


@dataclass
class TimeSource:
    name: str
    rows: list[TimeRow] = field(default_factory=list)
    # 時間ではない行（研究ログにある実験の要約など）
    skipped: int = 0
    problems: list[str] = field(default_factory=list)
    # 写したあとに呼ぶ（手元の記録を「送った」にする）
    mark: Callable[[str], None] | None = None


@dataclass(frozen=True)
class TimeReport:
    name: str
    # 写した（確認だけのときは、写す）記録 ID
    copied: tuple[str, ...]
    present: int
    skipped: int
    problems: tuple[str, ...]


def _child_source(notion: Notion, parent_id: str, title: str) -> str | None:
    """親ページ直下の、その名前の DB の data source。無ければ None、重複していれば止める。"""
    matches = [b["id"] for b in notion.children(parent_id)
               if b.get("type") == "child_database" and b.get("child_database", {}).get("title") == title]
    if len(matches) > 1:
        raise NotionError(f"「{title}」が重複しています。正本を確認してください")
    if not matches:
        return None
    sources = notion.request("GET", f"/databases/{matches[0]}").get("data_sources") or []
    if len(sources) != 1:
        raise NotionError(f"「{title}」に data source が {len(sources)} 件あります")
    return sources[0]["id"]


def _old_rows(notion: Notion, source: TimeSource, ds_id: str, domain: str, label_name: str) -> None:
    """旧 DB（研究ログ・学習ログは同じ列の形）の行を、時間記録の行に直す。旧 DB には書かない。"""
    for row in notion.paginate("POST", f"/data_sources/{ds_id}/query", {"page_size": 100}):
        props = row.get("properties") or {}
        entry_id = plain_text((props.get("Kei Agent 記録ID") or {}).get("rich_text") or [])
        if not entry_id:
            source.skipped += 1
            continue
        started = ((props.get("日付") or {}).get("date") or {}).get("start") or ""
        minutes = (props.get("時間（分）") or {}).get("number")
        label = props.get(label_name) or {}
        try:
            datetime.fromisoformat(started)
            if not isinstance(minutes, int | float) or minutes <= 0:
                raise ValueError
        except ValueError:
            source.problems.append(f"{row.get('url') or row.get('id')}: 日付か時間（分）がありません")
            continue
        source.rows.append(TimeRow(
            entry_id, domain, plain_text(label.get("title") or label.get("rich_text") or []) or "-", started,
            max(1, round(minutes)), plain_text((props.get("メモ") or {}).get("rich_text") or []),
            (props.get("Slack") or {}).get("url") or ""))


def old_time_sources(notion: Notion, research_home: str, course_home: str,
                     course_state: Path) -> list[TimeSource]:
    """研究ホームの「研究ログ」と授業ホームの「学習ログ」を読む。学習ログは授業の状態ファイルを先に見る。"""
    research, course = TimeSource("研究ログ"), TimeSource("学習ログ")
    for source, domain, label, find in (
        (research, "research", "テーマ", lambda: _child_source(notion, research_home, "研究ログ")),
        (course, "course", "タイトル", lambda: _course_source(notion, course_home, course_state)),
    ):
        try:
            ds_id = find()
            if ds_id is None:
                source.problems.append(f"{source.name}が見つかりません")
                continue
            _old_rows(notion, source, ds_id, domain, label)
        except NotionError as e:
            source.problems.append(f"{source.name}を読めません: {e}")
    return [research, course]


def _course_source(notion: Notion, course_home: str, state_path: Path) -> str | None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return str(state["databases"]["study_logs"]["data_source_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return _child_source(notion, course_home, "学習ログ")


def local_work_source(store: Store) -> TimeSource:
    """Slack で測った仕事の時間のうち、Notion に書いていないもの（前は仕事を Notion に書かなかった）。"""
    rows = store.unsent_work_time_entries()
    return TimeSource("手元の仕事の記録", [
        TimeRow(r["id"], "work", r["channel_name"], datetime.fromtimestamp(r["started_at"]).astimezone().isoformat(),
                max(1, int((r["ended_at"] - r["started_at"] + 59) // 60)), r["memo"])
        for r in rows], mark=lambda entry_id: store.set_time_delivery(entry_id, notion_state="done"))


def copy_time(hub: HubStore, sources: list[TimeSource], apply: bool) -> list[TimeReport]:
    """記録 ID をそのまま使って時間記録に写す。もう入っている ID は触らない（何度実行してもよい）。

    記録 ID は Slack の計測ごとの ID で、領域をまたいで重ならない。いまの仕組みが同じ記録を送り直しても
    同じ行に入るので、頭に印は付けない。
    """
    starts = [datetime.fromisoformat(row.started_at).date() for source in sources for row in source.rows]
    known = hub.time_ids_since(min(starts) - timedelta(days=1)) if starts else set()
    seen: set[str] = set()
    reports = []
    for source in sources:
        copied, present, problems = [], 0, list(source.problems)
        for row in source.rows:
            if row.entry_id in seen:
                problems.append(f"{row.entry_id}: 記録 ID が重複しています")
                continue
            seen.add(row.entry_id)
            if row.entry_id in known:
                present += 1
                continue
            if apply:
                try:
                    hub.record_time(row.entry_id, row.domain, row.label, row.started_at, row.minutes,
                                    row.memo, row.slack_url, "Slack")
                except (NotionError, ValueError) as e:
                    problems.append(f"{row.entry_id}: {e}")
                    continue
                if source.mark:
                    source.mark(row.entry_id)
            copied.append(row.entry_id)
        reports.append(TimeReport(source.name, tuple(copied), present, source.skipped, tuple(problems)))
    return reports


LOCAL_LABELS = {"copied": "写す", "appended": "末尾に足す", "present": "もうある", "problem": "問題"}


def _print_local(report: list[LocalCopy], overview_dir: Path, apply: bool) -> None:
    print(f"{'適用' if apply else '確認だけ'}: {overview_dir} の Daily・レトプラ")
    for item in report:
        print(f"  {LOCAL_LABELS[item.status]}\t{item.path}" + (f"（{item.detail}）" if item.detail else ""))
    counts = {status: sum(item.status == status for item in report) for status in LOCAL_LABELS}
    print("、".join(f"{label} {counts[status]} 件" for status, label in LOCAL_LABELS.items())
          + ("。ローカルのファイルは消していません" if apply else "。書き込むには --apply を付けてください"))


def _print_time(reports: list[TimeReport], apply: bool) -> None:
    print(f"{'適用' if apply else '確認だけ'}: 時間記録への移行")
    for r in reports:
        print(f"  {r.name}: {'写した' if apply else '写す'} {len(r.copied)} 件、もうある {r.present} 件、"
              f"時間ではない行 {r.skipped} 件、問題 {len(r.problems)} 件")
        for problem in r.problems:
            print(f"    - {problem}")
    print("旧 DB は消していません" if apply else "書き込むには --apply を付けてください")


def _other_modes(args, config) -> None:
    """--local と --time。どちらも共通ホームの接続（load_hub）で読み書きする。"""
    hub = load_hub(config)
    if hub is None:
        sys.exit("共通 Notion ホームを利用できません。kei-agent-hub-setup と共有を確認してください")
    try:
        if args.local:
            report = copy_local_notes(hub, config.overview_dir, args.apply)
            _print_local(report, config.overview_dir, args.apply)
            failed = any(item.status == "problem" for item in report)
        else:
            sources = old_time_sources(hub.notion, config.notion.research_home, config.notion.course_home,
                                       config.state_dir / "notion-course.json")
            reports = copy_time(hub, [*sources, local_work_source(Store(config.db_path))], args.apply)
            _print_time(reports, args.apply)
            failed = any(r.problems for r in reports)
    except (NotionError, OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"移行を停止しました: {error}")
    if args.apply and failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-hub-migrate")
    parser.add_argument("--manifest", type=Path, help="監査結果の JSON（既定は state_dir 配下）")
    parser.add_argument("--apply", action="store_true", help="確認した内容で書き込む（既定は確認だけ）")
    parser.add_argument("--expected-count", type=int, help="適用前に確認した元ページ件数（研究 Notes の移行だけ）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true",
                      help="overview/daily と overview/reviews のファイルを日別記録へ写す")
    mode.add_argument("--time", action="store_true",
                      help="旧研究ログ・旧学習ログ・手元の仕事の記録を時間記録へ写す")
    args = parser.parse_args()
    config = load_config()
    if args.local or args.time:
        _other_modes(args, config)
        return
    path = args.manifest or config.state_dir / "hub-migration-manifest.json"
    try:
        notion = gateway_notion("kei-agent", config=config)
    except NotionError as error:
        parser.error(str(error))
    if not args.apply:
        research = NotionStore(notion, config.state_dir / "notion.json")
        manifest = audit_legacy_notes(notion, research.state["databases"]["notes"]["data_source_id"])
        _save_manifest(path, manifest)
        print(f"監査完了: {len(manifest.entries)} 件。manifest: {path}")
        return
    if args.expected_count is None or args.expected_count < 0:
        parser.error("--apply には --expected-count が必要です")
    if not path.exists():
        parser.error(f"manifest がありません: {path}。先に dry-run を実行してください")
    try:
        manifest = MigrationManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if len(manifest.entries) != args.expected_count:
            raise NotionError(f"manifest は {len(manifest.entries)} 件で、確認済み件数と一致しません")
        hub = load_hub(config)
        if hub is None:
            raise NotionError("共通 Notion ホームを利用できません。setup と共有を確認してください")
        verify_manifest_current(notion, manifest, hub.state.home_id)
        report = apply_legacy_notes(notion, hub, manifest, Store(config.db_path), config.notion.research_home)
    except (NotionError, OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"移行を停止しました: {error}")
    print(f"コピー {report.copied} 件、移動 {report.moved} 件。未完了: {', '.join(report.unresolved) or 'なし'}")
    if report.unresolved:
        sys.exit(1)
