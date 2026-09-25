"""研究 Notes の Daily／振り返りを共通ホームへ移す一回限りの監査と移行。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from kei_agent.config import load_config
from kei_agent.notion import Notion, NotionError, write_json_atomic
from kei_agent.notion_hub import RESEARCH_HOME_ID, HubStore, load_hub
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
    file: str | None
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
            plain_text(props.get("ファイル", {}).get("rich_text") or []) or None,
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
    return "\n".join(line.strip() for line in re.sub(r"\*\*|`", "", value).splitlines() if line.strip())


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


def _archive_research_view(notion: Notion) -> None:
    """対象 block を一意に同定できるときだけ旧表示を外す。ノート DB 自体は残す。"""
    blocks = notion.children(RESEARCH_HOME_ID)
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
                       store: Store) -> MigrationReport:
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
            hub.upsert_day(kind, day, latest.title, text, latest.slack_url, latest.file)
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
    _archive_research_view(notion)
    return MigrationReport(copied, moved, ())


def _save_manifest(path: Path, manifest: MigrationManifest) -> None:
    write_json_atomic(path, manifest.to_dict())


def main() -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-hub-migrate")
    parser.add_argument("--manifest", type=Path, help="監査結果の JSON（既定は state_dir 配下）")
    parser.add_argument("--apply", action="store_true", help="検証済み manifest をコピー・移動に適用する")
    parser.add_argument("--expected-count", type=int, help="適用前に確認した元ページ件数")
    args = parser.parse_args()
    config = load_config()
    path = args.manifest or config.state_dir / "hub-migration-manifest.json"
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        parser.error("NOTION_TOKEN がありません")
    notion = Notion(token)
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
        report = apply_legacy_notes(notion, hub, manifest, Store(config.db_path))
    except (NotionError, OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"移行を停止しました: {error}")
    print(f"コピー {report.copied} 件、移動 {report.moved} 件。未完了: {', '.join(report.unresolved) or 'なし'}")
    if report.unresolved:
        sys.exit(1)
