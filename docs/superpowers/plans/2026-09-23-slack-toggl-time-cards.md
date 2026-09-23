# Slack Toggl Time Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Let the owner record one research, university, or work interval from fixed Slack cards, write a completed Toggl entry on stop, and mirror research/university records to their authorized Notion homes.

**Architecture:** time_tracking.py owns durable SQLite timer and delivery state. time_cards.py owns Block Kit and delegates university operations to course A2A and research operations to a typed, authenticated Notion Gateway endpoint. Toggl receives one completed taskless entry after stopping.

**Tech Stack:** Python 3.13, sqlite3, Slack Bolt AsyncApp/Block Kit, aiohttp, Toggl Focus 2.0 API, existing A2A service, existing typed Notion gateway, pytest.

**Spec:** docs/superpowers/specs/2026-09-23-slack-toggl-time-cards-design.md

## Global Constraints

- Keep exactly one active timer per allowed Slack user; starting a new one atomically finishes the preceding local interval.
- Never call or poll Toggl at start. Stop sends exactly one completed taskless Focus entry.
- The Slack service must never read NOTION_COURSE_TOKEN or NOTION_TOKEN.
- University writes use course A2A; research writes use the loopback Gateway; work never writes Notion.
- Bind courses by Slack channel ID. Never infer course identity from a short channel name.
- Slack bots cannot manage pins: post/update the card and ask the user to pin it manually.
- Ambiguous Toggl writes are needs_review and never automatic retries. Notion writes retry idempotently by Kei Agent 記録ID.
- Do not log credentials, raw Notion bodies, or academic record content. Preserve the user-owned dirty files.

---

## File Structure

- Create src/kei_agent/time_tracking.py: TimeEntry, TimerContext, labels, and state machine.
- Create src/kei_agent/time_cards.py: card blocks, modals, actions, and delivery coordinator.
- Modify src/kei_agent/store.py: timer/card/course-binding/delivery schemas and transactions.
- Modify src/kei_agent/timelog.py: Focus POST transport, project resolution, completed write.
- Modify src/kei_agent_course/notion_setup.py, notion_sync.py, card.py, executor.py, course.py: current-course and 学習ログ A2A skills.
- Modify src/kei_agent_notion_gateway/service.py and app.py; create src/kei_agent/time_log_clients.py: narrow research-log endpoint and client.
- Modify src/kei_agent/assistant.py and app.py: compose controller and register only kei_agent_time_ interactions.
- Modify src/kei_agent_course/toggl_report.py: accept 大学 / course projects.
- Create src/kei_agent/time_cards_setup.py: idempotent discovery and posting CLI.
- Modify pyproject.toml, slack/manifest.yaml, deploy/README.md, docs/using.md.
- Create tests/test_time_tracking.py, tests/test_time_cards.py, tests/test_time_log_clients.py, tests/test_time_cards_setup.py and extend existing Toggl/course/gateway tests.

### Task 1: Durable timer state

**Files:**
- Create: src/kei_agent/time_tracking.py
- Modify: src/kei_agent/store.py
- Test: tests/test_time_tracking.py

**Interfaces:**
- Produces TimerContext(user_id, domain, channel_id, channel_name, course_page_id="", course_name="").
- Produces TimeTracker.start(context, started_at) -> tuple[TimeEntry, TimeEntry | None], stop(user_id, ended_at) -> TimeEntry | None, add_memo(entry_id, memo), and active(user_id).
- Produces Store methods start_time_entry, finish_time_entry, active_time_entry, time_entry, pending_time_entries, set_time_delivery, upsert_time_card, time_card, bind_course_channel, and course_binding.

- [ ] **Step 1: Write failing state-machine tests**

~~~
def test_starting_another_timer_finishes_the_previous_one(store):
    tracker = TimeTracker(store)
    first, replaced = tracker.start(TimerContext("U1", "research", "C10", "amr"), 100.0)
    second, replaced = tracker.start(TimerContext("U1", "work", "C30", "work"), 130.0)

    assert replaced.id == first.id and replaced.ended_at == 130.0
    assert tracker.active("U1").id == second.id

def test_reopening_store_restores_unfinished_timer(tmp_path):
    tracker = TimeTracker(Store(tmp_path / "state.db"))
    entry, _ = tracker.start(TimerContext("U1", "course", "C20", "course"), 100.0)

    restored = TimeTracker(Store(tmp_path / "state.db")).active("U1")
    assert restored.id == entry.id and restored.ended_at is None
~~~

- [ ] **Step 2: Verify failure**

Run: uv run pytest tests/test_time_tracking.py -q

Expected: FAIL because the timer model and Store API do not exist.

- [ ] **Step 3: Add atomic Store operations**

Add time_cards, course_channel_bindings, time_entries, and active_timers to SCHEMA. Time entry IDs are UUID strings; delivery states are pending, done, needs_review, and not_required. start_time_entry must, in one SQLite transaction, finish the previous active record, insert the new entry, and replace active_timers. finish_time_entry stamps end only once and deletes only the active pointer.

- [ ] **Step 4: Add the pure state-machine boundary**

~~~
@dataclass(frozen=True)
class TimerContext:
    user_id: str
    domain: Literal["research", "course", "work"]
    channel_id: str
    channel_name: str
    course_page_id: str = ""
    course_name: str = ""

class TimeTracker:
    def start(self, context: TimerContext, started_at: float | None = None) -> tuple[TimeEntry, TimeEntry | None]: ...
    def stop(self, user_id: str, ended_at: float | None = None) -> TimeEntry | None: ...
~~~

Set work records to notion_state=not_required. Reject blank IDs and trim memos to 1,000 characters.

- [ ] **Step 5: Verify and commit**

Run: uv run pytest tests/test_time_tracking.py tests/test_store.py -q

Expected: PASS for replacement, restart recovery, memo, binding, and immutable completed entries.

~~~
git add src/kei_agent/time_tracking.py src/kei_agent/store.py tests/test_time_tracking.py
git commit -m "Slack時間記録の永続状態を追加する"
~~~

### Task 2: Completed Toggl writes and course-report compatibility

**Files:**
- Modify: src/kei_agent/timelog.py
- Modify: src/kei_agent_course/toggl_report.py
- Test: tests/test_timelog.py
- Test: tests/test_course_toggl_report.py

**Interfaces:**
- Produces Toggl.record_completed(project_name, description, started_at, duration_seconds).
- Produces project_label(domain, label) as exactly 研究 / label, 大学 / label, or 仕事 / label.

- [ ] **Step 1: Write failing transport/report tests**

~~~
def test_completed_entry_resolves_project_then_posts_one_entry(toggl, transport):
    toggl.record_completed("大学 / マルチメディア工学A", "大学 / マルチメディア工学A", START, 1500)
    assert transport.calls[-1] == (
        "POST", "/organizations/7/workspaces/8/time-entries/bulk",
        {"items": [{"project_id": 41, "description": "大学 / マルチメディア工学A",
                    "start": START.isoformat(), "duration": 1500, "type": "activity"}]},
    )

def test_course_report_matches_prefixed_project():
    assert totals([entry(project="大学 / データベース", duration=3600)], {"データベース"}) == {"データベース": 3600}
~~~

- [ ] **Step 2: Verify failure**

Run: uv run pytest tests/test_timelog.py tests/test_course_toggl_report.py -q

Expected: FAIL because completed writes and prefix normalization do not exist.

- [ ] **Step 3: Implement Focus POSTs**

Refactor the GET-only helper to _request(method, path, body=None), support JSON and 204 No Content, and retain the existing read API. Paginate project lookup by exact name; POST the minimum project body when missing and cache its numeric ID in-process. Implement one POST to /organizations/{org}/workspaces/{workspace}/time-entries/bulk with an items list containing project_id, description, timezone-aware start, integer duration, and activity type. Do not include user_id, task_id, or a blank memo.

Connection/timeout after dispatch raises TogglAmbiguousWrite; validation/auth failures raise TogglError. Normalize only a leading 大学 / in course aggregation so legacy project names remain valid.

- [ ] **Step 4: Verify and commit**

Run: uv run pytest tests/test_timelog.py tests/test_course_toggl_report.py -q

Expected: PASS for reads, project reuse/creation, exact outgoing body, 204, ambiguity, legacy, and prefixed totals.

~~~
git add src/kei_agent/timelog.py src/kei_agent_course/toggl_report.py tests/test_timelog.py tests/test_course_toggl_report.py
git commit -m "Togglへ完了した時間記録を書き込めるようにする"
~~~

### Task 3: University A2A skills and 学習ログ

**Files:**
- Modify: src/kei_agent_course/notion_setup.py
- Modify: src/kei_agent_course/notion_sync.py
- Modify: src/kei_agent_course/card.py
- Modify: src/kei_agent_course/executor.py
- Modify: src/kei_agent/course.py
- Test: tests/test_course_sync.py
- Test: tests/test_a2a.py

**Interfaces:**
- Produces list-current-courses and record-study-time A2A skills.
- Produces notion_sync.current_courses(on) and record_study_time(entry_id, started_at, duration_minutes, course_page_id, memo, slack_url).
- record-study-time returns entry_id and notion_url in the common envelope data.

- [ ] **Step 1: Write failing course tests**

~~~
def test_current_courses_exclude_finished_and_out_of_term_rows(course_notion):
    assert [r["subject"] for r in course_notion.current_courses(date(2026, 9, 23))] == ["履修中の科目"]

def test_record_study_time_is_idempotent_and_relates_course(course_notion, notion):
    course_notion.record_study_time("entry-1", START, 25, "course-page", "復習", "https://slack.example/p1")
    course_notion.record_study_time("entry-1", START, 25, "course-page", "復習", "https://slack.example/p1")
    assert len(_posts_to(notion, "/pages")) == 1
~~~

- [ ] **Step 2: Verify failure**

Run: uv run --group course pytest tests/test_course_sync.py tests/test_a2a.py -q

Expected: FAIL because both skills and 学習ログ are absent.

- [ ] **Step 3: Implement the deterministic destination**

Add study_logs to CourseSetup.SPECS. 学習ログ has タイトル, Kei Agent 記録ID, 日付, 時間（分）, メモ, Slack, and optional dual 科目 relation to courses. Existing homes are upgraded through ensure_study_logs. Query the record ID before create; use the canonical course name as title when present and 大学の学習 otherwise.

Expose list-current-courses using the existing blank/履修中 plus periods.in_term filtering. Expose record-study-time with validation: nonblank ID, positive bounded whole minutes, optional course ID, 1,000-character memo, and HTTPS Slack URL. Run blocking Notion calls in asyncio.to_thread.

- [ ] **Step 4: Verify and commit**

Run: uv run --group course pytest tests/test_course_sync.py tests/test_a2a.py -q

Expected: PASS for filtering, relation, idempotency, envelope data, and assignment regressions.

~~~
git add src/kei_agent_course/notion_setup.py src/kei_agent_course/notion_sync.py src/kei_agent_course/card.py src/kei_agent_course/executor.py src/kei_agent/course.py tests/test_course_sync.py tests/test_a2a.py
git commit -m "大学の学習時間をNotionへ記録できるようにする"
~~~

### Task 4: Narrow research-gateway time log

**Files:**
- Modify: src/kei_agent_notion_gateway/service.py
- Modify: src/kei_agent_notion_gateway/app.py
- Create: src/kei_agent/time_log_clients.py
- Test: tests/test_notion_gateway.py
- Test: tests/test_time_log_clients.py

**Interfaces:**
- Produces ResearchNotion.record_time(entry_id, started_at, duration_minutes, theme, memo, slack_url).
- Produces authenticated POST /time-logs returning only entry_id and notion_url.
- Produces ResearchTimeLogClient.record(entry, slack_url).

- [ ] **Step 1: Write failing gateway tests**

~~~
def test_record_time_creates_once_inside_research_home(service, notion):
    service.record_time("entry-1", START, 25, "amr", "実装", "https://slack.example/p1")
    service.record_time("entry-1", START, 25, "amr", "実装", "https://slack.example/p1")
    assert len([call for call in notion.calls if call[1] == "/pages"]) == 1

async def test_time_logs_endpoint_requires_bearer(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        assert (await client.post("/time-logs", json={})).status_code == 401
~~~

- [ ] **Step 2: Verify failure**

Run: uv run pytest tests/test_notion_gateway.py tests/test_time_log_clients.py -q

Expected: FAIL because typed storage, endpoint, and client are absent.

- [ ] **Step 3: Implement only the scoped operation**

ensure_time_logs discovers or creates 研究ログ under scope.root_id. Its properties are タイトル, Kei Agent 記録ID, 日付, 時間（分）, テーマ, メモ, and Slack. Cache its ID in existing research state JSON via atomic gateway-private writing. Query record ID before create. Every Notion call goes through _begin and _call, so scope and metadata-only audit rules apply.

POST /time-logs accepts exactly entry_id, ISO started_at, positive duration_minutes, theme, optional memo, and slack_url; invalid/extra input returns short 400 JSON. The client authenticates with the existing gateway token and logs no request body.

- [ ] **Step 4: Verify and commit**

Run: uv run pytest tests/test_notion_gateway.py tests/test_time_log_clients.py -q

Expected: PASS for auth, scope, validation, idempotency, and payload-free audit logs.

~~~
git add src/kei_agent_notion_gateway/service.py src/kei_agent_notion_gateway/app.py src/kei_agent/time_log_clients.py tests/test_notion_gateway.py tests/test_time_log_clients.py
git commit -m "研究時間をNotion Gateway経由で記録する"
~~~

### Task 5: Fixed Slack cards and delivery coordination

**Files:**
- Create: src/kei_agent/time_cards.py
- Modify: src/kei_agent/assistant.py
- Modify: src/kei_agent/app.py
- Modify: tests/fakes.py
- Create: tests/test_time_cards.py
- Modify: tests/test_assistant.py

**Interfaces:**
- Produces TimeCardController.on_action(body), on_view_submission(body), ensure_card(channel_id, channel_name), and retry(entry_id, user_id).
- Registers action IDs beginning kei_agent_time_ and callback IDs kei_agent_time_course_picker and kei_agent_time_memo.

- [ ] **Step 1: Write failing controller tests**

~~~
async def test_start_then_stop_updates_one_card_and_writes_toggl(slack, controller, toggl):
    await controller.on_action(press("kei_agent_time_start", channel="C10", user="U1"))
    await controller.on_action(press("kei_agent_time_stop", channel="C10", user="U1"))
    assert len(slack.posts()) == 1
    assert toggl.completed == 1

async def test_starting_elsewhere_finishes_old_before_new(controller, tracker):
    await controller.on_action(press("kei_agent_time_start", channel="C10", user="U1"))
    await controller.on_action(press("kei_agent_time_start", channel="C30", user="U1"))
    assert tracker.finished_entries()[0].channel_id == "C10"

async def test_other_user_cannot_operate_card(slack, controller):
    await controller.on_action(press("kei_agent_time_start", user="UNAUTHORIZED"))
    assert not slack.calls_named("chat_update")
~~~

- [ ] **Step 2: Verify failure**

Run: uv run pytest tests/test_time_cards.py tests/test_assistant.py -q

Expected: FAIL because cards and actions do not exist.

- [ ] **Step 3: Render and parse fixed cards**

Render labels only as 開始, 停止, メモを追加, 授業を選んで開始, and 再送. Put context in section text, not labels; show stored start time rather than a polling countdown. 20_course opens a picker filled by list-current-courses. Mapped individual course channels start immediately; unmapped ones create a general university record. Action values carry only IDs, never tokens or course titles.

- [ ] **Step 4: Implement stop ordering and delivery states**

Stop locally first, update affected cards, write Toggl, then call exactly one course A2A or research gateway writer. Work never invokes a Notion writer. Retry pending Notion entries on controller construction and a later allowed action. Ambiguous Toggl results offer 再送 only after an explicit allowed-user press. Extend FakeSlack for all asserted calls.

- [ ] **Step 5: Register safely**

Compose the controller in Assistant. Register action regex through existing acked wrapper, and modal callbacks with ack-first validation. Network work must happen after Slack acknowledgement and route exceptions through notify_trouble without raw service output.

- [ ] **Step 6: Verify and commit**

Run: uv run pytest tests/test_time_tracking.py tests/test_time_cards.py tests/test_assistant.py tests/test_time_log_clients.py -q

Expected: PASS for authorization, card reuse, replacement order, picker, memo, work/no-Notion, retry, and restart recovery.

~~~
git add src/kei_agent/time_cards.py src/kei_agent/assistant.py src/kei_agent/app.py tests/fakes.py tests/test_time_cards.py tests/test_assistant.py
git commit -m "Slackの固定時間記録カードを追加する"
~~~

### Task 6: Setup CLI, manifest, documentation, and verification

**Files:**
- Create: src/kei_agent/time_cards_setup.py
- Modify: pyproject.toml
- Modify: slack/manifest.yaml
- Modify: deploy/README.md
- Modify: docs/using.md
- Test: tests/test_time_cards_setup.py
- Create: tests/test_manifest.py

**Interfaces:**
- Produces kei-agent-time-cards setup and kei-agent-time-cards status.
- Produces discover_enabled_channels(slack, config) and setup_cards(controller, channels).

- [ ] **Step 1: Write failing setup tests**

~~~
async def test_setup_posts_one_card_per_accessible_numbered_channel(slack, controller):
    result = await setup_cards(controller, await discover_enabled_channels(slack, config))
    assert result.created == ["10_amr", "20_course", "30_work"]
    assert "固定" in result.instructions

def test_manifest_keeps_required_scopes():
    scopes = yaml.safe_load(Path("slack/manifest.yaml").read_text())["oauth_config"]["scopes"]["bot"]
    assert {"channels:read", "groups:read", "chat:write"} <= set(scopes)
~~~

- [ ] **Step 2: Verify failure**

Run: uv run pytest tests/test_time_cards_setup.py tests/test_manifest.py -q

Expected: FAIL because setup CLI is absent.

- [ ] **Step 3: Implement idempotent setup**

Paginate conversations_list with public_channel,private_channel and exclude_archived. Keep bot-member channels beginning 10_, 20_, or 30_; update stored cards instead of duplicating. Report inaccessible channels as skipped. status exposes only channel names and card state. Add the console script. Keep existing read/write scopes; do not add a nonexistent bot pin scope. Document manual pinning and required Slack reauthorization after manifest changes.

- [ ] **Step 4: Run full checks**

Run: uv run --group course pytest tests/test_time_tracking.py tests/test_time_cards.py tests/test_time_cards_setup.py tests/test_time_log_clients.py tests/test_timelog.py tests/test_course_toggl_report.py tests/test_course_sync.py tests/test_a2a.py tests/test_notion_gateway.py -q

Expected: PASS.

Run: uv run ruff check src tests && uv run python -m compileall -q src && uv run --group course pytest -q

Expected: PASS, or report exact pre-existing failures separately.

- [ ] **Step 5: Inspect, commit, and await deployment authorization**

Run: git diff --check && git diff main~6..HEAD -- src/kei_agent src/kei_agent_course src/kei_agent_notion_gateway slack/manifest.yaml

Expected: no credentials, raw Notion logging, course token leakage, work-to-Notion route, or whitespace failures.

~~~
git add src/kei_agent/time_cards_setup.py pyproject.toml slack/manifest.yaml deploy/README.md docs/using.md tests/test_time_cards_setup.py tests/test_manifest.py
git commit -m "時間記録カードのセットアップを追加する"
~~~

Deploy only after explicit user authorization:

~~~
./deploy/install.sh && ./deploy/healthcheck.sh
kei-agent-time-cards setup
~~~

Then manually pin the posted cards in Slack and perform one non-sensitive start, stop, and memo smoke test.

