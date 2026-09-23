# Retro Slack 出力境界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retro & Planning の Slack 投稿から作業実況とローカルパスを排除し、検証済みの最終フォーマットだけを投稿する。

**Architecture:** review 専用の parser がモデルの result text を全体検証し、受理済みの `RunResult.text` だけを `Assistant.publish()` に渡す。footer は Notion page の有無から作る純粋関数にし、Codex App とファイルシステムを利用者向け copy から除外する。

**Tech Stack:** Python 3.12、asyncio、Slack Bolt adapter、pytest。

**Spec:** `docs/superpowers/specs/2026-09-23-execution-boundary-cleanup-design.md`

## Global Constraints

- Slack に通す Retro 本文は `*今日の成果*`、`*未完了タスク*`、固定の最終質問だけとする。
- progress narration、tool/skill 名、余分な見出し、ローカル絶対パスを投稿しない。
- 不正な出力を切り貼り・推測して救済しない。定型の失敗結果として再実行可能にする。
- Notion が失敗しても review スレッドは作り、案内は Slack スレッドだけにする。
- コミット・push は依頼者の明示指示があるまで行わない。

## Review Focus

- 正しい見出しの前に一行でも実況が付く入力を Task 1 で拒否する。
- `*今日の成果*` の内容が `なし` の最小出力を Task 1 で受理する。
- 最終質問の表記揺れ・余計な末尾文を Task 1 で拒否する。
- Notion page が作れない経路を Task 2 で Slack-only copy に固定する。
- `RunResult.is_error` が既に true のとき本文 validator で別エラーを上書きしないことを Task 2 で検証する。

---

## File Structure

- Create: `src/kei_agent/review_output.py` — Retro final text の parse/validate と footer copy。
- Modify: `src/kei_agent/schedule.py` — `run_review()` で validator を通してから publish。
- Modify: `tests/test_schedule.py` — user-visible Slack 投稿の回帰。
- Modify: `docs/design.md`, `docs/notion-layout.md` — review file は内部材料、Slack は Notion/thread だけを案内することを明記。

### Task 1: strict review parser を作る

**Files:**
- Create: `src/kei_agent/review_output.py`
- Create: `tests/test_review_output.py`

**Interfaces:**
- Produces: `ReviewOutputError(ValueError)`。
- Produces: `validate_review_reply(text: str) -> str`。
- Produces: `review_footer(notion_url: str | None, title: str) -> str`。

- [ ] **Step 1: allowed output と実況混入の failing test を書く**

```python
from kei_agent.review_output import ReviewOutputError, validate_review_reply

VALID = """*今日の成果*
なし

*未完了タスク*
なし

夜間に実行したいタスクはありますか？"""

def test_accepts_exact_review_contract():
    assert validate_review_reply(VALID) == VALID

def test_rejects_progress_narration_before_contract():
    text = "まず材料を確認します。\n" + VALID
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply(text)

def test_rejects_extra_footer_after_contract():
    with pytest.raises(ReviewOutputError):
        validate_review_reply(VALID + "\nCodex App を開いてください")
```

- [ ] **Step 2: failure を確認する**

Run: `uv run --group dev --group agents pytest tests/test_review_output.py -q`

Expected: module がないため FAIL。

- [ ] **Step 3: whole-text validator を実装する**

```python
HEADINGS = ("*今日の成果*", "*未完了タスク*")
NIGHT_QUESTION = "夜間に実行したいタスクはありますか？"

def validate_review_reply(text: str) -> str:
    normalized = text.strip().replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    if len(parts) != 3 or not parts[0].startswith(HEADINGS[0] + "\n"):
        raise ReviewOutputError("Retro の返答が指定形式ではありません")
    if not parts[1].startswith(HEADINGS[1] + "\n") or parts[2] != NIGHT_QUESTION:
        raise ReviewOutputError("Retro の返答が指定形式ではありません")
    if any(line.startswith("#") for line in normalized.splitlines()):
        raise ReviewOutputError("Retro の返答が指定形式ではありません")
    return normalized
```

内容行は空にせず、見出し以外の Markdown heading・絶対パス・`Codex App` を拒否する test も追加する。validator は先頭探索や部分抽出をしない。

- [ ] **Step 4: footer copy の test と実装を足す**

```python
def test_review_footer_never_mentions_codex_app_or_path():
    footer = review_footer("https://notion.so/review", "Retro & Planning 9/23（水）")
    assert "Codex App" not in footer and "/Users/" not in footer
    assert "このスレッド" in footer and "https://notion.so/review" in footer

def test_review_footer_without_notion_uses_thread_only():
    assert review_footer(None, "Retro") == "振り返りの結論があれば、このスレッドに書いてね。明日の Daily に反映するよ。"
```

Notion 有りの copy は `振り返りの結論があれば、このスレッドか Notion の <URL|title> に書いてね。明日の Daily に反映するよ。` とする。

- [ ] **Step 5: parser unit suite を通す**

Run: `uv run --group dev --group agents pytest tests/test_review_output.py -q`

Expected: PASS。

### Task 2: scheduler publish 経路に validator を組み込む

**Files:**
- Modify: `src/kei_agent/schedule.py`
- Modify: `tests/test_schedule.py`

**Interfaces:**
- Consumes: `validate_review_reply()` and `review_footer()` from Task 1。
- Produces: `run_review(day: str) -> dict` が validated text だけを Slack/Notion へ渡す。

- [ ] **Step 1: schedule の failing integration test を書く**

```python
async def test_review_never_posts_model_progress_narration(env):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": "材料を読みます。\n" + VALID}]
    result = await scheduler.run_review("2026-09-23")
    assert result["status"] == "error"
    assert all("材料を読みます" not in text for text in slack.texts())
    assert "指定形式" in slack.texts()[-1]

async def test_review_footer_is_notion_native_without_local_path(env):
    scheduler, assistant, slack, claude = env
    claude.behaviors = [{"text": VALID, "side_effect": write_review_file(scheduler, "2026-09-23")}]
    await scheduler.run_review("2026-09-23")
    assert "Codex App" not in slack.texts()[-1]
    assert "/reviews/" not in slack.texts()[-1]
```

- [ ] **Step 2: failing integration test を確認する**

Run: `uv run --group dev --group agents pytest tests/test_schedule.py::test_review_never_posts_model_progress_narration tests/test_schedule.py::test_review_footer_is_notion_native_without_local_path -q`

Expected: 現実装は raw text と Codex App footer を投稿するため FAIL。

- [ ] **Step 3: `run_review` の success result を変換する**

```python
result = await self.assistant.run_detached(
    ws, self.overview_channel_name, prompt, "review", actor="router", use_case=UseCase.OVERVIEW_PLAN,
)
if not result.is_error:
    try:
        result.text = validate_review_reply(result.text)
    except ReviewOutputError as exc:
        result.is_error = True
        result.errors.append(str(exc))
        result.text = ""

thread_ts = await self.assistant.publish(
    channel, self.overview_channel_name, ws, f"🌙 Retro & Planning {label(day)}", result,
)
await self.assistant.post(
    Request(channel, self.overview_channel_name, thread_ts, None, ""),
    review_footer(note.url if note else None, title),
)
```

`review_path` は Markdown/Notion 保存の内部処理だけに残し、footer を決める根拠に使わない。すでに `is_error` の result は validator を呼ばない。

- [ ] **Step 4: 既存 review test を期待仕様へ更新する**

既存の `assert "Codex App で" in texts[2]` を削り、Notion URL・このスレッド・Daily 反映の文言、絶対パス不在を検証する。正常出力 fixture を `VALID` に置換する。

- [ ] **Step 5: schedule suite を通す**

Run: `uv run --group dev --group agents pytest tests/test_schedule.py tests/test_review_output.py -q`

Expected: PASS。

### Task 3: 利用者向け設計文書を同期して全体確認する

**Files:**
- Modify: `docs/design.md`
- Modify: `docs/notion-layout.md`

- [ ] **Step 1: docs regression test を書く**

```python
def test_review_docs_do_not_instruct_users_to_open_a_local_path():
    text = Path("docs/notion-layout.md").read_text(encoding="utf-8")
    assert "Codex App で `" not in text
    assert "このスレッド" in text
```

- [ ] **Step 2: docs test を failure で確認する**

Run: `uv run --group dev --group agents pytest tests/test_docs_contract.py -q`

Expected: 古い案内が残っていれば FAIL。

- [ ] **Step 3: internal artifact と user-facing copy を書き分ける**

`reviews/<day>.md` は Notion 投影・翌日の材料・agent が読む内部 artifact と説明する。利用者の action は Slack thread または Notion page だけと明記する。

- [ ] **Step 4: verification を実行する**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run --group dev --group agents pytest tests/test_schedule.py tests/test_review_output.py -q && UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uvx ruff check src/kei_agent/review_output.py src/kei_agent/schedule.py && git diff --check`

Expected: すべて成功、diff check は出力なし。
