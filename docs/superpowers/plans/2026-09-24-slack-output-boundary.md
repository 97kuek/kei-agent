# Slack 出力境界の統一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude、Codex、A2A のどの経路でも、Slack には Kei Agent の最終回答だけを安全な口調と形式で投稿する。

**Architecture:** `response_output.py` を Slack 表示直前の唯一の境界にする。model の raw text は final marker と投稿種別の契約で検証し、会話・Daily・Retro を個別に render する。A2A と `ThreadUI` は raw text を status に出さず、固定進捗と利用者向け失敗文だけを表示する。Moodle の秋冬科目は専用の読み取り検査で既存の授業DBと比較し、変更を起こさない。

**Tech Stack:** Python 3.13、pytest、Slack Bolt API、Claude Code / Codex CLI、Notion API、Moodle ICS。

**Spec:** `docs/superpowers/specs/2026-09-24-slack-output-boundary-design.md`

## Global Constraints

- 返答の model / provider 選択は既存 `ResolvedModel` recipe 以外から決めない。
- Slack に raw model text、tool / skill / CLI 名、local path、file URI、exception detail、secret を出さない。
- Daily 見出しは `**太字**`、Retro 見出しも `**太字**` とする。
- 既存 session は捨てず、次の request から更新済み prompt 規則を渡す。
- Moodle ICS の新規科目は、曜日・時限・履修状態を推測して Notion へ書き込まない。
- API token、Moodle export URL、Notion の本文・成績・メール本文をテスト出力やコミットへ含めない。

## Review Focus

- 通常の Web URL を local path と誤認せず、Slack 本文に残す。
- final marker 外に正しい回答があっても一切 Slack に出さない。
- `❓ 確認:` / `🧵 区切り:` / `🔒 接続:` は final marker 内で既存の制御を維持する。
- `chat.startStream` が失敗して通常投稿へ戻っても、同じ検証済み本文だけを表示する。
- Moodle に未知科目があっても、Notion の course / assignment / relation を変更しない。

---

## File Structure

- Create: `src/kei_agent/response_output.py` — final marker の抽出、通常会話・Daily・Retro の検証、表示可能な失敗文。
- Modify: `src/kei_agent/assistant.py` — Slack 投稿前の renderer、error renderer、定期投稿種別の受け渡し。
- Modify: `src/kei_agent/thread_ui.py` — raw model text / command を status に出さない固定 progress。
- Modify: `src/kei_agent/course.py`, `src/kei_agent/work.py` — A2A free-form reply を conversation renderer へ通す。
- Modify: `src/kei_agent/schedule.py` — Daily / Retro の strict renderer と太字 prompt。
- Modify: `src/kei_agent_a2a/claude.py` — raw text を A2A progress に送らない。
- Modify: `prompts/system.md`, `prompts/course.md`, `prompts/work.md` — provider に共通の final marker 契約。
- Create: `src/kei_agent_course/catalog.py` — ICS の科目名と授業DB既存名を読み取り専用で比較する pure logic。
- Modify: `src/kei_agent_course/moodle.py`, `src/kei_agent_course/notion_sync.py`, `pyproject.toml` — read-only catalog inspection と CLI entry point。
- Modify: `docs/design.md`, `docs/using.md`, `docs/voice.md` — output boundary とユーザーの既存文書変更を統合。
- Create/Modify: `tests/test_response_output.py`, `tests/test_assistant.py`, `tests/test_schedule.py`, `tests/test_course_sync.py`, `tests/test_catalog.py`。

### Task 1: Pure Slack response contracts

**Files:**
- Create: `src/kei_agent/response_output.py`
- Create: `tests/test_response_output.py`
- Modify: `src/kei_agent/review_output.py`
- Modify: `tests/test_review_output.py`

**Interfaces:**
- Produces `OutputError(ValueError)`, `finalize_conversation(text: str) -> str`, `validate_daily(text: str) -> str`, `validate_review(text: str) -> str`, `safe_failure(kind: str) -> str`.
- `finalize_conversation` accepts exactly one final region, discards all text outside it, and rejects forbidden internal content inside the rendered region.
- `validate_daily` accepts exactly four non-empty double-asterisk sections in the specified order.

- [ ] **Step 1: Write failing contract tests**

```python
def test_finalizer_keeps_only_the_marked_user_facing_answer():
    raw = "まず材料を確認します。\n<<kei-agent-final>>\n僕が調べた結果、課題はないよ。\n<<kei-agent-final-end>>"
    assert finalize_conversation(raw) == "僕が調べた結果、課題はないよ。"

def test_finalizer_rejects_missing_marker_and_local_path():
    with pytest.raises(OutputError):
        finalize_conversation("確認します")
    with pytest.raises(OutputError):
        finalize_conversation("<<kei-agent-final>>\nreviews/today.md に保存したよ\n<<kei-agent-final-end>>")

def test_daily_requires_four_bold_sections():
    assert validate_daily("**今日のタスク**\nなし\n\n**夜間処理の結果**\nなし\n\n**確認待ち・期日・止まっているテーマ・返事待ち**\nなし\n\n**今日考えるとよい問い**\n1. 何から進める？")
    with pytest.raises(OutputError):
        validate_daily("*今日のタスク*\nなし")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run --group dev --group agents pytest -q tests/test_response_output.py tests/test_review_output.py`

Expected: FAIL because `response_output` does not exist and Retro still accepts single-asterisk headings.

- [ ] **Step 3: Implement the parser without model calls**

```python
FINAL_OPEN = "<<kei-agent-final>>"
FINAL_CLOSE = "<<kei-agent-final-end>>"
DAILY_HEADINGS = (
    "**今日のタスク**",
    "**夜間処理の結果**",
    "**確認待ち・期日・止まっているテーマ・返事待ち**",
    "**今日考えるとよい問い**",
)

def finalize_conversation(text: str) -> str:
    parts = text.strip().split(FINAL_OPEN)
    if len(parts) != 2:
        raise OutputError("final marker")
    body, close, tail = parts[1].partition(FINAL_CLOSE)
    if not close or tail.strip() or not body.strip() or _forbidden(body):
        raise OutputError("conversation contract")
    return body.strip()
```

Keep Web URLs valid while rejecting absolute paths, `~/`, `file://`, internal relative paths, tool / skill names, and progress narration. Move the shared detection from `review_output.py`; keep a compatibility import only if callers still import that module.

- [ ] **Step 4: Run focused tests**

Run: `uv run --group dev --group agents pytest -q tests/test_response_output.py tests/test_review_output.py`

Expected: PASS.

- [ ] **Step 5: Commit the isolated contract layer**

```bash
git add src/kei_agent/response_output.py src/kei_agent/review_output.py tests/test_response_output.py tests/test_review_output.py
git commit -m "feat: add slack response contracts"
```

### Task 2: Render every user-facing provider reply through the contract

**Files:**
- Modify: `src/kei_agent/assistant.py:437-465,719-735,992-1005`
- Modify: `src/kei_agent/course.py:175-210`
- Modify: `src/kei_agent/work.py:115-145`
- Modify: `src/kei_agent/thread_ui.py:40-105`
- Modify: `src/kei_agent_a2a/claude.py:76-105`
- Modify: `tests/test_assistant.py`
- Modify: `tests/test_course_sync.py`

**Interfaces:**
- Consumes `finalize_conversation` and `safe_failure` from Task 1.
- Produces `Assistant.render_reply(result: RunResult) -> tuple[str, bool]`, where the bool reports a contract failure.

- [ ] **Step 1: Write failing integration tests**

```python
async def test_assistant_never_posts_raw_model_preamble(assistant, request, monkeypatch):
    monkeypatch.setattr(assistant, "run_agent", returns(
        RunResult(text="まず調べます\n<<kei-agent-final>>\n僕が調べた結果、できたよ。\n<<kei-agent-final-end>>")))
    await assistant.process(request)
    assert "まず調べます" not in assistant.slack.posted_text()
    assert "僕が調べた結果、できたよ。" in assistant.slack.posted_text()

async def test_assistant_uses_safe_copy_when_final_contract_is_missing(env, monkeypatch):
    async def invalid(*_args, **_kwargs):
        return runner.RunResult(text="材料を確認してから返します")
    monkeypatch.setattr(env.assistant, "run_agent", invalid)
    await env.assistant.process(Request("C1", "vlm", "1.1", "1.1", "質問", trigger="message"))
    assert "返答を利用者向けの形に整えられなかったよ" in env.slack.posted_text()

async def test_thread_status_never_uses_raw_model_text(slack):
    ui = ThreadUI(slack, "C1", "1.1", "T1", "U1")
    await ui.text("Bash で /private/secret を確認します")
    assert "Bash" not in slack.status_text()
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `uv run --group dev --group agents pytest -q tests/test_assistant.py -k 'raw_model_preamble or safe_copy or status' tests/test_course_sync.py`

Expected: FAIL because `_reply`, course/work free-form paths, and `ThreadUI.text` pass raw text through.

- [ ] **Step 3: Add a single rendering boundary**

Implement `Assistant.render_reply` and use it before `ThreadUI.finish`, `append_thread_log`, and fallback `post`. Do not change raw `result.text` before internal marker detection, handoff extraction, or audit logging; use the rendered value only for Slack output. For conversation contract failure, set the user-facing content to `safe_failure("conversation")` and keep `result.is_error` false so the UI does not append raw provider errors.

Replace `ThreadUI.text` with a call to `_thinking("まとめている…")`. Normalize `ThreadUI.activity` to one of `"調べている…"`, `"作業している…"`, or `"まとめている…"`; do not retain command strings or paths. Remove `on_text` from `kei_agent_a2a.claude.run` progress entirely.

Route `CourseChannel.course_ask` and `WorkChannel.work_ask` through the same `render_reply` before streaming or posting.

- [ ] **Step 4: Run integration and regression tests**

Run: `uv run --group dev --group agents pytest -q tests/test_assistant.py tests/test_course_sync.py tests/test_agent_claude.py tests/test_handoff.py`

Expected: PASS.

- [ ] **Step 5: Commit the shared display boundary**

```bash
git add src/kei_agent/assistant.py src/kei_agent/course.py src/kei_agent/work.py src/kei_agent/thread_ui.py src/kei_agent_a2a/claude.py tests/test_assistant.py tests/test_course_sync.py tests/test_agent_claude.py tests/test_handoff.py
git commit -m "fix: keep provider internals out of slack"
```

### Task 3: Apply strict Daily and Retro rendering

**Files:**
- Modify: `src/kei_agent/schedule.py:292-388`
- Modify: `tests/test_schedule.py`
- Modify: `prompts/system.md`

**Interfaces:**
- Consumes `validate_daily`, `validate_review`, and `safe_failure` from Task 1.
- `Assistant.publish(channel: str, channel_name: str, ws: Workspace, header: str, result: RunResult, footer: str = "", output_kind: str = "conversation")` validates the supplied `RunResult.text` before Slack or Notion write.

- [ ] **Step 1: Write failing schedule tests**

```python
async def test_daily_posts_only_four_bold_sections(env):
    env.runner.reply_text = "手順を確認します\n<<kei-agent-final>>\n**今日のタスク**\nなし\n\n**夜間処理の結果**\nなし\n\n**確認待ち・期日・止まっているテーマ・返事待ち**\nなし\n\n**今日考えるとよい問い**\n1. 僕は何から進めよう？\n<<kei-agent-final-end>>"
    await env.schedule.run_daily("2026-09-24")
    assert "手順を確認" not in env.slack.thread_text()
    assert "**今日のタスク**" in env.slack.thread_text()

async def test_invalid_daily_is_not_saved_to_notion(env):
    env.runner.reply_text = "<<kei-agent-final>>\n*今日のタスク*\nなし\n<<kei-agent-final-end>>"
    result = await env.schedule.run_daily("2026-09-24")
    assert result["status"] == "error"
    assert env.notion.created_notes == []
```

- [ ] **Step 2: Run the Daily tests to verify they fail**

Run: `uv run --group dev --group agents pytest -q tests/test_schedule.py -k 'daily_posts_only or invalid_daily'`

Expected: FAIL because Daily asks for single-asterisk headings and publishes raw text.

- [ ] **Step 3: Implement strict routine rendering**

Change the Daily prompt to require final markers, four `**...**` headings, no implementation narration, and the existing Kei Agent "僕 / 〜だよ" copy. Change Retro to markers plus `**今日の成果**` / `**未完了タスク**`; retain the exact final question. Validate before `publish`, before thread logging, and before `_save_note`. On validation failure, do not write the model text to Notion; publish only `safe_failure("daily")` or `safe_failure("review")`.

Update `prompts/system.md` with the final marker rule and explicitly state that planning, tool use, skill invocation, file save messages, and environment errors are internal and must not appear inside the final region.

- [ ] **Step 4: Run schedule and contract tests**

Run: `uv run --group dev --group agents pytest -q tests/test_schedule.py tests/test_response_output.py tests/test_review_output.py`

Expected: PASS.

- [ ] **Step 5: Commit routine rendering**

```bash
git add src/kei_agent/schedule.py prompts/system.md tests/test_schedule.py
git commit -m "fix: enforce daily and retro slack contracts"
```

### Task 4: Make the final-marker instruction provider-wide

**Files:**
- Modify: `prompts/course.md`
- Modify: `prompts/work.md`
- Modify: `docs/agents.md`
- Modify: `tests/test_docs_contract.py`

**Interfaces:**
- All user-facing model prompts use the exact `<<kei-agent-final>>` / `<<kei-agent-final-end>>` pair.

- [ ] **Step 1: Write failing prompt contract tests**

```python
def test_every_user_facing_agent_prompt_requires_only_a_final_region():
    for path in ("prompts/system.md", "prompts/course.md", "prompts/work.md"):
        text = Path(path).read_text()
        assert "<<kei-agent-final>>" in text
        assert "<<kei-agent-final-end>>" in text
        assert "作業手順" in text and "Slack に出さない" in text
```

- [ ] **Step 2: Run the prompt tests to verify they fail**

Run: `uv run --group dev --group agents pytest -q tests/test_docs_contract.py -k final_region`

Expected: FAIL because the course and work prompts do not define the marker.

- [ ] **Step 3: Update prompt and agent documentation**

Add the exact marker section to course and work prompts. Keep existing evidence/citation requirements inside the final region. Update `docs/agents.md` to state that A2A returns raw output to the orchestrator, and only the orchestrator renders Slack output.

- [ ] **Step 4: Run documentation contract tests**

Run: `uv run --group dev --group agents pytest -q tests/test_docs_contract.py`

Expected: PASS.

- [ ] **Step 5: Commit provider-neutral prompts**

```bash
git add prompts/course.md prompts/work.md docs/agents.md tests/test_docs_contract.py
git commit -m "docs: define final slack response protocol"
```

### Task 5: Read-only Moodle autumn/winter course inspection

**Files:**
- Create: `src/kei_agent_course/catalog.py`
- Modify: `src/kei_agent_course/moodle.py`
- Modify: `src/kei_agent_course/notion_sync.py`
- Modify: `pyproject.toml`
- Create: `tests/test_catalog.py`
- Modify: `docs/agents/course.md`

**Interfaces:**
- Produces `CourseCatalog(registered: tuple[str, ...], known: tuple[str, ...], missing: tuple[str, ...])`.
- Produces `compare_course_catalog(events: Sequence[Event], known_names: Iterable[str]) -> CourseCatalog` with no I/O or writes.
- Produces `notion_sync.course_catalog(notion: Notion, state: dict) -> tuple[str, ...]`, which only reads the `courses` data source.
- Adds `kei-agent-course-inspect` CLI: fetch ICS, read the existing `授業` DB, print only counts and course names; never posts, creates, updates, or deletes Notion data.

- [ ] **Step 1: Write failing pure and integration tests**

```python
def test_catalog_deduplicates_ics_course_names_without_writes():
    events = [
        Event("a", "提出期限", None, "情報通信ネットワークB"),
        Event("b", "提出期限", None, "情報通信ネットワークB"),
        Event("c", "提出期限", None, "統計解析実習"),
    ]
    report = compare_course_catalog(events, {"情報通信ネットワークB"})
    assert report.registered == ("情報通信ネットワークB", "統計解析実習")
    assert report.known == ("情報通信ネットワークB",)
    assert report.missing == ("統計解析実習",)

def test_catalog_inspection_never_calls_notion_request_with_write_method(fake_notion):
    notion_sync.course_catalog(fake_notion, {"databases": {"courses": {"data_source_id": "courses"}}})
    assert all(method in {"GET", "POST"} and "/query" in path for method, path, _ in notion.calls)
```

- [ ] **Step 2: Run the catalog tests to verify they fail**

Run: `uv run --group dev --group agents pytest -q tests/test_catalog.py`

Expected: FAIL because no catalog module or read-only command exists.

- [ ] **Step 3: Implement read-only inspection**

Use `moodle.due()` as the only ICS fetch and extract non-empty `Event.course_name` values. Query the existing course data source through `CourseNotion` / `notion_sync` without mutation. Sort Japanese names deterministically, print `Moodle 登録科目`, `授業DB にある科目`, and `確認が必要な科目` sections. Do not call `sync`, `add_course`, or any page/database mutation from this command.

- [ ] **Step 4: Run catalog and course regressions**

Run: `uv run --group dev --group agents pytest -q tests/test_catalog.py tests/test_course_sync.py tests/test_course_ics.py`

Expected: PASS.

- [ ] **Step 5: Commit inspection support**

```bash
git add src/kei_agent_course/catalog.py src/kei_agent_course/moodle.py src/kei_agent_course/notion_sync.py pyproject.toml tests/test_catalog.py tests/test_course_sync.py tests/test_course_ics.py docs/agents/course.md
git commit -m "feat: inspect moodle course catalog safely"
```

### Task 6: Integrate existing main documentation, verify, deploy, and inspect Moodle

**Files:**
- Modify: `README.md`, `CONTRIBUTING.md`, `docs/using.md`, `docs/voice.md`, `docs/design.md`
- Modify: `docs/superpowers/specs/2026-09-24-slack-output-boundary-design.md`

**Interfaces:**
- Consumes the user-authored uncommitted main documentation changes and the output boundary documentation from Tasks 1-5.
- Produces a clean local `main` at the reviewed branch tip and launchd services registered from `/Users/keitaro/src/kei-agent`.

- [ ] **Step 1: Preserve and integrate the existing main changes**

Capture `git -C /Users/keitaro/src/kei-agent diff --binary` to an explicitly named temporary patch under `/private/tmp`, then apply the non-conflicting README / CONTRIBUTING / using changes to this branch. Resolve `docs/voice.md` by preserving the user’s prose simplifications and removals while retaining the current fixed realtime model and provider-neutral `ask_agent` design. Do not discard or silently rewrite any user-authored text.

- [ ] **Step 2: Add documentation merge tests/checks**

Run: `git diff --check && rg -n 'gpt-5\.6|Codex App.*Voice|ask_research|think\.py' README.md CONTRIBUTING.md docs src tests`

Expected: `git diff --check` passes; any historical references are either removed or explicitly labelled historical without describing current behavior.

- [ ] **Step 3: Run all verification**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run --group dev --group agents pytest -q`

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uvx ruff check .`

Expected: full suite and static check PASS.

- [ ] **Step 4: Commit, fast-forward local main, and push**

```bash
git add README.md CONTRIBUTING.md docs/using.md docs/voice.md docs/design.md docs/superpowers/specs/2026-09-24-slack-output-boundary-design.md
git commit -m "docs: unify user guides with output policy"
git -C /Users/keitaro/src/kei-agent merge --ff-only codex/use-case-model-policy
git -C /Users/keitaro/src/kei-agent push origin main
```

Expected: main is clean and `origin/main` points at the verified commit.

- [ ] **Step 5: Deploy and verify every service**

Run from `/Users/keitaro/src/kei-agent`:

```zsh
./deploy/install.sh
./deploy/install.sh course
./deploy/install.sh research
./deploy/install.sh work
./deploy/install.sh voice
./deploy/install.sh notion-gateway
./deploy/healthcheck.sh
```

Expected: every `com.kei-agent.*` service is running; no secret appears in output.

- [ ] **Step 6: Inspect Moodle only after the deployed command is available**

Run from `/Users/keitaro/src/kei-agent`, sourcing the existing course service secrets without printing them:

```zsh
source ~/.config/zsh/local/kei-agent-course.zsh
uv run --group course kei-agent-course-inspect
```

Expected: a read-only list of registered autumn/winter course names, existing `授業` DB names, and candidates requiring confirmation. Do not run `kei-agent-course-sync --all` or `kei-agent-course-setup --seed`.

## Self-Review

- Spec coverage: Tasks 1–4 cover final responses, routines, A2A/status, errors, provider-neutral prompts, and regression tests. Task 5 covers the read-only Moodle requirement. Task 6 preserves the approved main docs, validates, deploys, and inspects the live catalog.
- Placeholder scan: no task uses TBD/TODO or an unspecified test/implementation step.
- Type consistency: Task 1 defines all renderer functions consumed in Tasks 2–3. Task 5 defines `CourseCatalog` and `compare_course_catalog` before its CLI use.
- Review focus coverage: Web URL and marker handling are Task 1 tests; control markers and stream fallback are Task 2 tests; Moodle unknown course mutation is Task 5 test; all are repeated in full verification.

## Execution Handoff

Plan complete. The user has already authorized worktree execution, main push, and deployment. After review confirmation, implement inline with `superpowers:executing-plans`, then request one whole-branch review before merging.
