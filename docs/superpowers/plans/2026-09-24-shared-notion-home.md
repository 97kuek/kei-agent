# 共通 Notion ホーム Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Keitaro Ueki` ページを研究・授業・仕事の横断ハブにし、今週の Task、予定カレンダー、日別 Daily／レトプラを重複・欠落なく運用する。

**Architecture:** 研究 Task と授業課題は既存 DB を正本のままリンクドビューで表示する。新しい `HubStore` は本体専用 Notion 接続でカレンダーと日別記録を管理し、各 domain agent へは必要な読み取り結果だけを渡す。migration は dry-run／manifest／照合／旧ページの保管を分離し、既存リンクを壊さない。

**Tech Stack:** Python 3.13、uv、pytest、ruff、Notion API、A2A、SQLite、Outlook connector。

**Spec:** `docs/superpowers/specs/2026-09-24-shared-notion-home-design.md`

## Global Constraints

- 研究・授業ホームは物理的に移動せず、既存 DB と agent 別接続を正本のまま保つ。
- 親ページの ID は `3e54fb5d2d07808dbe29fdbe67a4de56`。見つからない場合は新しいページを推測で作らない。
- `日別記録` は1日1行。表示列は `日付`、`Daily`、`レトプラ` の順。全文は行本文の二つの区画に置く。
- 既存 Daily／振り返り10件の本文・URL・同日重複を失わず、旧ページは削除しない。
- Outlook／Moodle／課題の元データは読取のみ。同期失敗で既存カレンダー行を削除・空欄化しない。
- provider の暗黙の切替をしない。`NOTION_TOKEN` や course token を agent prompt、Slack、通常ログへ出さない。
- コミット・push・デプロイは依頼者の明示依頼まで行わない（共通 AGENTS.md が計画内の commit 手順より優先）。
- Provider parity の未解決の権限検証と、この Notion 変更の統合・デプロイは別判定にする。

## Review Focus

- 9/21 の振り返りが2件ある入力で、日別行が1件でも本文・元 URL が2件とも残ること（Task 3）。
- 予定を30日取得したつもりで A2A の一部だけ返った入力で、欠落行を `要確認` にしないこと（Tasks 4–5）。
- 同じ時刻・同じ件名の別 Outlook event と、手入力の行を誤って結合・上書きしないこと（Task 5）。
- Daily の再実行と Slack 結論の追記が前後しても、レトプラ本文・結論が残ること（Task 2）。
- 親ページの Notion 共有がない環境で、研究ホームへ誤保存せず理由を返すこと（Tasks 1, 6）。

---

## File Structure

- Create `src/kei_agent/notion_hub.py`: hub schema/state/preflight と日別記録の read/write。研究 `NotionStore` の責務を増やさない。
- Create `src/kei_agent/notion_hub_migration.py`: 旧ノートの監査・manifest・コピー照合・旧ページ移動。通常運用から呼ばない。
- Create `src/kei_agent/calendar_sync.py`: domain snapshot をカレンダー row に変換し、出典 ID で upsert。Notion の低レベル通信を `HubStore` に委譲。
- Modify `src/kei_agent_course/executor.py`, `src/kei_agent_course/notion_sync.py`, `src/kei_agent_course/card.py`: 課題 DB から完全な read-only snapshot を A2A で返す。Moodle ICS の20件上限を再利用しない。
- Modify `src/kei_agent_work/connector.py`, `src/kei_agent_work/executor.py`: Outlook event ID と完全性を、既存 `list-events` の data に追加する。
- Modify `src/kei_agent/schedule.py`, `src/kei_agent/assistant.py`, `src/kei_agent/digest.py`, `src/kei_agent/notion_store.py`: Daily／レトプラの正本切替、結論追記、ハブ同期の起動と読み取り。
- Modify `src/kei_agent/config.py`, `src/kei_agent/app.py`, `docs/notion-layout.md`, `docs/design.md`, `deploy/README.md`: hub state、起動時 schema check、共有手順、運用説明。
- Test `tests/test_notion_hub.py`, `tests/test_notion_hub_migration.py`, `tests/test_calendar_sync.py`, `tests/test_a2a.py`, `tests/test_course_sync.py`, `tests/test_work_agent.py`, `tests/test_schedule.py`, `tests/test_notion_store.py`, `tests/test_assistant.py`。

## Task 1: ハブ schema・正本確認・リンクドビュー

**Files:** Create `src/kei_agent/notion_hub.py`, `tests/test_notion_hub.py`; modify `src/kei_agent/config.py`。

**Interfaces:** Produces `HubState(home_id: str, calendar_ds_id: str, daily_ds_id: str)`, `HubSetup(notion: Notion, home_id: str, state_path: Path).inspect() -> list[str]`, `.run() -> HubState`, `HubStore(notion: Notion, state: HubState)` for later tasks. `run()` is idempotent; duplicate title or inaccessible target raises `NotionError` before any write.

- [ ] **Step 1: failing tests** — `tests/test_notion_hub.py` に `FakeHubNotion` fixture を定義し、`request()`／`paginate()`／`children()` と `writes` を持たせる。親ページ、既存 `今月の予定`、研究 Task、授業課題を置く。`inspect()` が存在／所有関係を確認し、同名カレンダー2件または親ページへの権限なしで停止するテストを書く。`run()` 2回で `日別記録` と各リンクドビューが1個ずつになること、研究・授業ホームの parent ID が変わらないことを検証する。例:

```python
def test_hub_setup_rejects_duplicate_calendar_before_writing(fake_notion, tmp_path):
    fake_notion.add_child_database("home", "今月の予定", "db-a")
    fake_notion.add_child_database("home", "今月の予定", "db-b")
    setup = HubSetup(fake_notion, "home", tmp_path / "hub.json")
    with pytest.raises(NotionError, match="重複"):
        setup.run()
    assert fake_notion.writes == []
```

- [ ] **Step 2: RED** — `uv run --group dev --group agents pytest tests/test_notion_hub.py -q` を実行し、`HubSetup` 未定義で失敗することを確認する。
- [ ] **Step 3: minimal implementation** — `HubSetup` は全対象 DB・view を先に列挙して重複／schema／アクセスを検査し、その後だけ不足分を作る。`日別記録` は `日付` title、`Daily`／`レトプラ` rich_text、`対象日` date、`Daily Slack`／`レトプラ Slack` URL、`移行元 ID` rich_text。`予定カレンダー` は既存 DB の ID を保持して `出典`・`出典 ID`・`元 URL`・`最終確認`・`同期状態` だけ追加する。リンクドビューは `POST /views` の `create_database.parent.page_id` を親ページに指定し、研究 Task の `期日 this_week`／状態、授業課題の `締切 this_week`／状態でフィルタする。各処理の ID と property 名は `hub.json` に保存する。
- [ ] **Step 4: GREEN** — 同じテストを通し、既存 `tests/test_notion_store.py tests/test_course_layout.py` も実行する。`git diff --check` を確認する。コミットはしない。

## Task 2: 日別記録の upsert と定期処理の切替

**Files:** Modify `src/kei_agent/notion_hub.py`, `src/kei_agent/schedule.py`, `src/kei_agent/assistant.py`, `src/kei_agent/digest.py`, `src/kei_agent/notion_store.py`; test `tests/test_notion_hub.py`, `tests/test_schedule.py`, `tests/test_assistant.py`, `tests/test_notion_store.py`。

**Interfaces:** Consumes `HubStore`. Produces `HubStore.upsert_day(kind: Literal["Daily", "振り返り"], day: str, title: str, markdown: str, slack_url: str | None, file: str | None) -> Note`, `.append_review_conclusion(page_id: str, text: str, stamp: datetime) -> None`, `.reviews_edited_since(since: datetime) -> list[Note]`, `.week_snapshot(today: date) -> str`, `.day_body(day: str) -> str`（読み取りとテストで使う）。

- [ ] **Step 1: failing tests** — 9/24 の Daily とレトプラを順不同で保存しても1行・2区画になること、同一 Daily の再実行でレトプラと結論を保持すること、要約列が2,000文字以内で空欄を偽の「なし」にしないことを検証する。`Scheduler.run_daily/run_review` と `Assistant.sync_review_conclusion` が新 row ID を使い、研究 `ノート` に新規 Daily／振り返りを作らないテストを書く。

```python
def test_upsert_day_preserves_the_other_section_and_conclusion(hub):
    row = hub.upsert_day("振り返り", "2026-09-24", "Retro", "元の振り返り", None, None)
    hub.append_review_conclusion(row.id, "決めたこと", datetime(2026, 9, 24, 21))
    hub.upsert_day("Daily", "2026-09-24", "Daily", "朝の内容", None, None)
    assert hub.day_body("2026-09-24") == "## Daily\n朝の内容\n\n## レトプラ\n元の振り返り\n\n### Slack に貼った結論（09/24 21:00）\n決めたこと"
```

- [ ] **Step 2: RED** — `uv run --group dev --group agents pytest tests/test_notion_hub.py tests/test_schedule.py tests/test_assistant.py -q` で新 API が未定義／旧 DB 書込のため失敗することを確認する。
- [ ] **Step 3: minimal implementation** — `HubStore` は対象日で最大1行を取得し、同一日が複数行なら書かず停止する。`Daily` と `レトプラ` の本文は分離した heading block 群で保持し、更新時は対象区画の block ID だけ置換する。結論はレトプラ末尾へ追記する。Scheduler は新 hub が利用可能なら `upsert_day` だけを使い、利用不可ならローカルファイルを残して Notion 未保存を通知する。`DigestBuilder` は研究ノートの計画・考察と新 DB の振り返りを併せて読み、移行中の旧レトプラを二重に載せない。`week_snapshot()` は必要な Task／課題／予定の日付・状態・URL だけを本体経由で返す。
- [ ] **Step 4: GREEN** — Task 2 の tests と `tests/test_review_output.py` を通す。ローカルファイルの既存保存テストを維持する。コミットはしない。

## Task 3: 旧記録の監査・無損失移行

**Files:** Create `src/kei_agent/notion_hub_migration.py`, `tests/test_notion_hub_migration.py`; modify `pyproject.toml`（`kei-agent-hub-migrate = "kei_agent.notion_hub_migration:main"` を追加）と `src/kei_agent/store.py`（対応 ID 更新）。

**Interfaces:** Consumes `HubStore.upsert_day`. Produces `audit_legacy_notes(notion: Notion, notes_ds_id: str) -> MigrationManifest`, `apply_legacy_notes(notion: Notion, hub: HubStore, manifest: MigrationManifest, store: Store) -> MigrationReport`。`MigrationManifest.entries` は元 page ID・URL・本文・日付・種類・Slack URL・ファイル・checksum の tuple、`MigrationReport` は `copied: int`, `moved: int`, `unresolved: tuple[str, ...]` を持つ。manifest は実行前に JSON 保存する。

- [ ] **Step 1: failing tests** — 10件（うち9/21のレトプラ2件）で日別行は対象日ごと、全文は10件分、元 URL はすべて残ることを検証する。日付なし・本文取得失敗・コピー後 checksum 不一致・旧ページ移動失敗では原本を消さず、`notion_links` の変更を行わない。再実行で行／本文が重複しないこと、完全移行後だけ研究ホームの `最近の Daily と振り返り` の見出し・リンクドビューを除くことを検証する。

```python
def test_two_reviews_on_one_day_are_preserved(fake_notion, hub, store):
    fake_notion.legacy_note("r1", "2026-09-21", "振り返り", "一つ目")
    fake_notion.legacy_note("r2", "2026-09-21", "振り返り", "二つ目")
    manifest = audit_legacy_notes(fake_notion, "notes-ds")
    report = apply_legacy_notes(fake_notion, hub, manifest, store)
    assert report.copied == 2
    assert "一つ目" in hub.day_body("2026-09-21")
    assert "二つ目" in hub.day_body("2026-09-21")
    assert fake_notion.deleted == []
```

- [ ] **Step 2: RED** — `uv run --group dev --group agents pytest tests/test_notion_hub_migration.py -q` で新関数未定義を確認する。
- [ ] **Step 3: minimal implementation** — `audit_legacy_notes` は全ページを pagination で読み、kind/date/body を厳格に検証し manifest を返す。`apply_legacy_notes` は対象日の本文へ元 ID を marker として付けて upsert、checksum と件数を再取得して照合、検証できた原本だけ `旧 Daily・レトプラ記録` へ移す。移動後も旧 ID／URL と本文を再取得して確認する。SQLite `notion_links` は全ての対応が確定してから transaction で新 row ID に更新する。全件移行できたときだけ研究ホームの旧 heading とリンクドビューの正確な block ID を確認して archive し、研究ノート DB 自体は残す。途中失敗は manifest に段階と残課題を書き、再実行可能にする。CLI は既定 dry-run、`--apply` だけ外部書込する。
- [ ] **Step 4: GREEN** — 新テストと `tests/test_notion_store.py tests/test_store.py` を通す。既存 notes の計画／考察が動くことを確認する。コミットはしない。

## Task 4: 大学担当から課題 DB の完全スナップショットを得る

**Files:** Modify `src/kei_agent_course/card.py`, `src/kei_agent_course/executor.py`, `src/kei_agent_course/notion_sync.py`; test `tests/test_a2a.py`, `tests/test_course_sync.py`。

**Interfaces:** Produces A2A skill `list-calendar-assignments` with `data={"complete": True, "items": [{"id": page_id, "title": str, "due": ISO8601, "status": str, "url": notion_url}]}`. Only reads course `課題` DB; no Moodle sync.

- [ ] **Step 1: failing tests** — `tests/test_course_sync.py` の既存 Notion fake に21件以上の課題と手入力課題を置き、全件が返り `complete=True` になることを検証する。`tests/test_a2a.py` では pagination 中のエラーが completed task でなく failed task になること、本文や course token が出力に入らないことを検証する。

```python
def test_calendar_assignment_snapshot_is_complete(fake_course_notion):
    for i in range(21):
        fake_course_notion.assignment(f"p-{i}", f"課題{i}", "2026-10-01T23:59:00+09:00")
    data = list_calendar_assignments(fake_course_notion, days=30, today=date(2026, 9, 24))
    assert data["complete"] is True
    assert len(data["items"]) == 21
```

- [ ] **Step 2: RED** — `uv run --group dev --group agents pytest tests/test_a2a.py tests/test_course_sync.py -q` で skill 未定義を確認する。
- [ ] **Step 3: minimal implementation** — `notion_sync.list_calendar_assignments(days: int, today: date) -> dict` を作り、`課題` data source の due 範囲で `paginate` を使う。ページ ID と URL を保持し、同じ ID の重複はエラーにする。executor と card に skill を登録する。A2A は既存の envelope で返し、失敗時は `complete=False` の成功レスポンスを返さず task を失敗させる。
- [ ] **Step 4: GREEN** — Task 4 の tests と `tests/test_course_report.py` を通す。コミットはしない。

## Task 5: Outlook・課題のカレンダー同期

**Files:** Create `src/kei_agent/calendar_sync.py`, `tests/test_calendar_sync.py`; modify `src/kei_agent_work/connector.py`, `src/kei_agent_work/executor.py`, `src/kei_agent/notion_hub.py`, `tests/test_work_agent.py`。

**Interfaces:** Consumes `HubStore`, A2A `list-calendar-assignments` and `list-events`. Produces `CalendarSnapshot(source: Literal["Outlook", "課題"], complete: bool, items: tuple[CalendarItem, ...])`, `sync_calendar(hub: HubStore, snapshot: CalendarSnapshot, checked_at: datetime) -> SyncReport`. `CalendarItem` contains `source_id`, `title`, `start`, `end`, `url`, `location`, `status`.

- [ ] **Step 1: failing tests** — Outlook の別 ID 同名予定2件と手入力1件を保存し、2回同期で行数が増えず手入力行が変更されないことを検証する。`complete=False`、A2A failure、Outlook JSON の件数超過／欠落では既存行の状態も日時も変わらないこと、完全 snapshot から消えた出典行は `要確認` となるが削除されないことを検証する。本文・参加リンクが出力に入らないテストを書く。

```python
def test_incomplete_snapshot_never_marks_existing_events_stale(hub):
    hub.seed_calendar("Outlook", "event-1", "会議", "2026-09-25T11:00")
    with pytest.raises(IncompleteSnapshot):
        sync_calendar(hub, CalendarSnapshot("Outlook", False, ()), datetime(2026, 9, 24))
    assert hub.calendar_row("Outlook", "event-1")["同期状態"] == "確認済み"
```

- [ ] **Step 2: RED** — `uv run --group dev --group agents pytest tests/test_calendar_sync.py tests/test_work_agent.py -q` で新 API 未定義／ID 欠落を確認する。
- [ ] **Step 3: minimal implementation** — work connector の `_event` に Outlook stable ID を残し、完全性を返せない応答は `complete=False` とする。`sync_calendar` は `source + source_id` で Notion 行を検索し、複数なら停止する。1出典分の snapshot を検証してから upsert し、最後に未見行だけ `要確認` にする。日付は Asia/Tokyo に正規化し、名前・時刻・場所・元 URL 以外の Outlook 内容を捨てる。手入力と別出典を更新しない。
- [ ] **Step 4: GREEN** — 新テスト、`tests/test_work_agent.py tests/test_calendar_sync.py tests/test_schedule.py` を通す。コミットはしない。

## Task 6: scheduler・運用手順・実接続の段階的適用

**Files:** Modify `src/kei_agent/schedule.py`, `src/kei_agent/app.py`, `src/kei_agent/config.py`, `src/kei_agent/assistant.py`, `docs/notion-layout.md`, `docs/design.md`, `deploy/README.md`; test `tests/test_schedule.py`, `tests/test_assistant.py`, `tests/test_notion_hub.py`。

**Interfaces:** Consumes Task 1–5. Produces `Scheduler.sync_hub_calendar(day: str) -> dict`, startup hub schema check, a read-only hub snapshot path for agent context, and explicit dry-run/apply commands for setup and migration.

- [ ] **Step 1: failing tests** — `tests/test_schedule.py` に既存 `env` fixture と `FakeHubStore` を組み合わせた `fake_scheduler` fixture を定義する。scheduler の1回実行で course/work の完全 snapshot を個別に同期し、一方が失敗しても他方と Daily 保存を継続するテストを書く。親ページアクセスなしでは Notion へ書かず利用者向けに不足を通知すること、agent へ渡す snapshot にトークンや担当外の編集能力が含まれないことを検証する。

```python
async def test_calendar_failure_does_not_stop_daily(fake_scheduler):
    fake_scheduler.work_snapshot_error = RuntimeError("Outlook unavailable")
    result = await fake_scheduler.sync_hub_calendar("2026-09-24")
    assert result["work"] == "error"
    assert result["course"] == "synced"
    await fake_scheduler.run_daily("2026-09-24")
    assert fake_scheduler.hub.saved_days == ["2026-09-24"]
```

- [ ] **Step 2: RED** — `uv run --group dev --group agents pytest tests/test_schedule.py tests/test_assistant.py -q` で hub 未接続のため失敗することを確認する。
- [ ] **Step 3: minimal implementation** — 起動時に hub state と schema を確認し、親ページが未共有なら hub だけ fail-closed で既存研究・授業 DB に書かない。定期スケジューラに独立した hub sync を登録し、`list-events(days=30)`／`list-calendar-assignments(days=30)` の A2A envelope を完全性確認後に変換する。読み取った共通 snapshot は本体から各 agent へ必要最小限の文脈として渡す。`docs/notion-layout.md` の旧 Daily／レトプラ正本と研究ホームの旧ビュー説明を更新し、`deploy/README.md` に親ページを本体 Notion integration に共有する手順、dry-run、manifest、apply、照合、復旧を書く。
- [ ] **Step 4: GREEN** — `uv run --group dev --group agents pytest -q --tb=short`、`uvx ruff check .`、`git diff --check` を実行し結果を読む。localhost test が sandbox で失敗するときだけ許可済みの escalated 実行を使う。コミットはしない。
- [ ] **Step 5: real Notion read-only preflight** — 親ページ、研究・授業 DB、旧記録の全件、現在の calendar view を再取得し、ID・件数・schema を manifest と照合する。`--apply` の対象件数と副作用を表示し、重複 DB・欠損・接続不可なら実行せず報告する。spec と計画の承認だけでは実 Notion の移行・同期を自動実行しない。実データへの適用は対象件数を確認して依頼者の承認を得た後、適用→再取得→照合と進める。コミット・push・デプロイは別途依頼がある場合だけ行う。
