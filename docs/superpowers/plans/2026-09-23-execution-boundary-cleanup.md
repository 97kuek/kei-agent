# 実行境界の統一とレガシー削除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** actor と use case から解決された model・権限だけで Claude/Codex を実行し、旧実行経路を除去する。

**Architecture:** `ResolvedModel` を policy の唯一の実行レシピにし、runner には actor 固有の `ExecutionRequest` を渡す。runner は request から plugin、MCP、sandbox、CLI 引数を組み立て、workspace や旧 profile を参照して権限やモデルを補わない。

**Tech Stack:** Python 3.12、asyncio、Claude CLI stream-json、Codex CLI JSONL、pytest、ruff。

**Spec:** `docs/superpowers/specs/2026-09-23-execution-boundary-cleanup-design.md`

## Global Constraints

- provider 間の自動 fallback を作らない。provider 未選択・quota は停止として呼び出し元へ返す。
- Codex は `gpt-6-luna`、`gpt-6-sol`、`gpt-6-astra`、Claude は `claude-haiku-4-5`、`claude-sonnet-5`、`claude-opus-5`、`claude-fable-5` だけを許可する。
- Astra/Fable は research actor の明示 manual use case だけで許可する。
- read-only 実行は workspace 初期化、Edit、write MCP、research Notion gateway を持たない。
- 既存の利用者変更を消さず、コミット・push は依頼者が明示的に頼むまで行わない。

## Review Focus

- router/self_fix が research plugin・research Notion gateway を誤って受け取らないことを Task 3 で command 単位に検証する。
- voice read-only の Claude command に `Bash`、`Edit`、`mcp__research-notion` が混ざらないことを Task 4 で検証する。
- actor と無関係な use case、manual Astra/Fable の別 actor 利用を Task 1 で拒否する。
- config の旧 model/effort 値や `Workspace` 上の値が、解決済み command を上書きできないことを Task 3 で検証する。
- CLI が quota に達した際、別 provider に移らず provider/model/use case/reset を保った停止結果になることを Task 4 で検証する。

---

## File Structure

- Modify: `src/kei_agent/model_policy.py` — actor/use case 許可表と安全な解決。
- Modify: `src/kei_agent/model_classifier.py` — classifier 専用の lightweight resolve 入口への移行。
- Modify: `src/kei_agent/runner.py` — `ExecutionRequest`、provider 固有 command、実行ループ。
- Modify: `src/kei_agent/guard.py` — request の read-only 制約から Claude settings を作る。
- Modify: `src/kei_agent/config.py`, `src/kei_agent/themes.py`, `src/kei_agent/settings.py`, `src/kei_agent/settings_actions.py` — legacy model/profile override の除去。
- Modify: `src/kei_agent/assistant.py`, `src/kei_agent/research.py`, `src/kei_agent/course.py`, `src/kei_agent/work.py`, `src/kei_agent/self_fix.py`, `src/kei_agent_a2a/claude.py`, `src/kei_agent_research/executor.py` — public request API への移行。
- Modify: `src/kei_agent_voice/handoff.py`, `src/kei_agent_voice/tools.py`; Delete: `src/kei_agent_voice/think.py` — read-only handoff だけへ統一。
- Modify: `tests/test_model_policy.py`, `tests/test_runner.py`, `tests/test_voice.py`, `tests/test_agent_claude.py`, `tests/test_research_agent.py`, `tests/test_settings.py`, `tests/test_themes.py` — 境界の回帰テスト。
- Modify: `tests/test_model_policy.py` — plugin actor の分類が通常 actor policy を迂回しない回帰。
- Modify: `docs/design.md`, `docs/agents.md`, `docs/agents/*.md`, `docs/model-policy.md`, `docs/voice.md` — 実装後の正本。

### Task 1: actor/use case policy を閉じる

**Files:**
- Modify: `src/kei_agent/model_policy.py`
- Modify: `tests/test_model_policy.py`

**Interfaces:**
- Produces: `allowed_use_cases(actor: str) -> frozenset[UseCase]`
- Produces: `resolve(actor: str, provider: str, use_case: UseCase | str, *, manual: bool = False) -> ResolvedModel`

- [ ] **Step 1: 不正な actor/use case と manual 例外の failing test を書く**

```python
@pytest.mark.parametrize(("actor", "case"), [
    ("router", UseCase.RESEARCH_EXECUTE),
    ("work", UseCase.COURSE_REQUIREMENTS),
    ("course", UseCase.MANUAL_ASTRA),
])
def test_resolve_rejects_use_case_outside_actor_policy(actor, case):
    with pytest.raises(ModelPolicyError):
        resolve(actor, "codex", case, manual=True)

def test_manual_fable_requires_research_and_claude():
    with pytest.raises(ModelPolicyError):
        resolve("research", "codex", UseCase.MANUAL_FABLE, manual=True)
```

- [ ] **Step 2: failing test を確認する**

Run: `uv run --group dev --group agents pytest tests/test_model_policy.py -q`

Expected: actor が違っても recipe を返すため FAIL。

- [ ] **Step 3: actor policy と provider 固有 manual 制約を実装する**

```python
ACTOR_USE_CASES = {
    "research": frozenset({UseCase.RESEARCH_EXTRACT, UseCase.RESEARCH_SCREEN,
                            UseCase.RESEARCH_COMPARE, UseCase.RESEARCH_EXECUTE,
                            UseCase.RESEARCH_DESIGN}),
    "course": frozenset({UseCase.COURSE_EXPLAIN, UseCase.COURSE_REQUIREMENTS,
                          UseCase.COURSE_COMPARE, UseCase.COURSE_DEGREE_PLAN}),
    "work": frozenset({UseCase.WORK_SINGLE_SOURCE, UseCase.WORK_CROSS_SOURCE,
                        UseCase.WORK_DECIDE}),
    "router": frozenset({UseCase.ROUTING, UseCase.OVERVIEW_DAILY, UseCase.OVERVIEW_PLAN}),
    "self_fix": frozenset({UseCase.SELF_FIX_DESIGN, UseCase.SELF_FIX_IMPLEMENTATION,
                             UseCase.SELF_FIX_REVIEW}),
}

def resolve(actor, provider, use_case, *, manual=False):
    case = UseCase(use_case)
    if case not in ACTOR_USE_CASES[actor]:
        raise ModelPolicyError(f"{actor} では {case.value} を使えません")
    # manual は research + 対応 provider + 対応 case 以外を拒否してから _MANUAL を読む
```

`UseCase.ROUTING` を research/course/work の分類器で使う必要があるため、別入口 `resolve_classifier(config, store, actor) -> ResolvedModel` を追加する。この入口だけが routing recipe を plugin actor に解決でき、通常の `resolve()` は actor policy 外を常に拒否する。`is_allowed_model()` は `ResolvedModel` を生成した直後に必ず検査し、allowlist 外なら `ModelPolicyError` にする。

- [ ] **Step 4: policy test を通す**

Run: `uv run --group dev --group agents pytest tests/test_model_policy.py -q`

Expected: PASS。

- [ ] **Step 5: 作業状態を確認する**

Run: `git diff --check`

Expected: 出力なし。コミットは依頼者が明示するまで行わない。

### Task 2: request を唯一の runner 入力にする

**Files:**
- Modify: `src/kei_agent/runner.py`
- Modify: `src/kei_agent/config.py`
- Modify: `src/kei_agent/themes.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_themes.py`

**Interfaces:**
- Consumes: `ResolvedModel` from Task 1。
- Produces: `ExecutionRequest(workspace: Workspace, recipe: ResolvedModel, session_id: str | None, channel: str, thread_ts: str, read_only: bool = False)`。
- Produces: `build_command(config: Config, request: ExecutionRequest) -> list[str]`。
- Produces: `run_model(config: Config, request: ExecutionRequest, prompt: str, on_activity: Callable | None = None, on_text: Callable | None = None) -> RunResult`。

- [ ] **Step 1: resolved recipe 以外を command に使わない failing test を書く**

```python
def test_command_uses_request_recipe_and_has_no_override_fields(config, workspace):
    recipe = resolve("research", "codex", UseCase.RESEARCH_DESIGN)
    request = ExecutionRequest(workspace, recipe, None, "C1", "1.1")
    assert {"model", "reasoning_effort"}.isdisjoint(request.__dataclass_fields__)
    command = runner.build_command(config, request)
    assert command[command.index("--model") + 1] == "gpt-6-sol"
    assert "model_reasoning_effort=xhigh" in command
```

- [ ] **Step 2: test が旧 API で FAIL することを確認する**

Run: `uv run --group dev --group agents pytest tests/test_runner.py -q`

Expected: `ExecutionRequest` が未定義、または旧 `build_command` signature により FAIL。

- [ ] **Step 3: `ExecutionRequest` と actor policy を runner に実装する**

```python
@dataclass(frozen=True)
class ExecutionRequest:
    workspace: Workspace
    recipe: ResolvedModel
    session_id: str | None
    channel: str
    thread_ts: str
    read_only: bool = False

def plugin_dir(config: Config, actor: str) -> Path | None:
    return config.agent_plugin_dir(actor) if actor in {"research", "course", "work"} else None

def build_command(config: Config, request: ExecutionRequest) -> list[str]:
    if not is_allowed_model(request.recipe.provider, request.recipe.model):
        raise ModelPolicyError("allowlist 外の model は実行できません")
    return (build_codex_command if request.recipe.provider == "codex" else build_claude_command)(config, request)
```

`Workspace` から `model`、`reasoning_effort`、`read_only` を削除し、`AgentProfile` は `provider` と `connectors` だけにする。`run_claude` と `_resolved_profile` は削除し、runner の公開実行入口を `run_model` のみにする。

- [ ] **Step 4: actor 別 command を追加検証する**

```python
def test_router_command_has_no_research_plugin_or_notion(config):
    recipe = resolve("router", "claude", UseCase.OVERVIEW_PLAN)
    command = runner.build_command(config, ExecutionRequest(router.workspace(config), recipe, None, "", ""))
    assert "research-notion" not in " ".join(command)
    assert "plugin/research" not in " ".join(command)

def test_research_command_has_only_research_plugin_and_gateway(config, workspace):
    recipe = resolve("research", "codex", UseCase.RESEARCH_EXECUTE)
    command = runner.build_command(config, ExecutionRequest(workspace, recipe, None, "C1", "1.1"))
    assert "research-notion" in " ".join(command)
```

- [ ] **Step 5: runner/theme tests を通す**

Run: `uv run --group dev --group agents pytest tests/test_runner.py tests/test_themes.py -q`

Expected: PASS。

### Task 3: read-only を Claude/Codex と A2A で強制する

**Files:**
- Modify: `src/kei_agent/guard.py`
- Modify: `src/kei_agent/runner.py`
- Modify: `src/kei_agent/assistant.py`
- Modify: `src/kei_agent/research.py`
- Modify: `src/kei_agent_a2a/claude.py`
- Modify: `src/kei_agent_research/executor.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_agent_claude.py`
- Modify: `tests/test_research_agent.py`

**Interfaces:**
- Consumes: `ExecutionRequest` from Task 2。
- Produces: `guard.build_settings(config: Config, request: ExecutionRequest) -> dict`。
- Produces: A2A payload field `read_only: bool` consumed without `themes.ensure_workspace()`.

- [ ] **Step 1: real read-only command/settings の failing tests を書く**

```python
def test_claude_read_only_settings_have_no_write_capability(config, workspace):
    request = ExecutionRequest(workspace, resolve("research", "claude", UseCase.RESEARCH_EXTRACT),
                               None, "", "", read_only=True)
    allowed = guard.build_settings(config, request)["permissions"]["allow"]
    assert not any("Edit(" in item or "research-notion" in item or item == "Bash" for item in allowed)

def test_codex_read_only_command_has_no_gateway(config, workspace):
    request = ExecutionRequest(workspace, resolve("research", "codex", UseCase.RESEARCH_EXTRACT),
                               None, "", "", read_only=True)
    command = runner.build_command(config, request)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "research-notion" not in " ".join(command)
```

- [ ] **Step 2: tests が既存の `Workspace.read_only` 経路で FAIL することを確認する**

Run: `uv run --group dev --group agents pytest tests/test_runner.py tests/test_agent_claude.py tests/test_research_agent.py -q`

Expected: Claude に Bash が残る、または A2A が workspace を初期化するため FAIL。

- [ ] **Step 3: request に基づく permission/MCP/A2A 実装へ置換する**

```python
def build_settings(config, request):
    allow = [*read_rules(config, request.workspace), "Glob", "Grep", "Skill"]
    if not request.read_only:
        allow += [edit_rule(request.workspace.cwd), "Bash", "WebSearch", "WebFetch"]
        if request.recipe.actor == "research":
            allow.append("mcp__research-notion")
    return {"sandbox": sandbox_settings(config, request), "permissions": {"allow": allow}}
```

Codex command は `request.read_only` または router actor のとき `--sandbox read-only` にし、research gateway は `actor == "research" and not request.read_only` のときだけ追加する。research A2A executor は read-only request で `themes.ensure_workspace()` を呼ばず、存在しない workspace を失敗として返す。

- [ ] **Step 4: quota と A2A payload の回帰 test を追加する**

```python
async def test_usage_limit_stops_without_provider_fallback(config, workspace, monkeypatch):
    request = ExecutionRequest(workspace, resolve("research", "claude", UseCase.RESEARCH_EXTRACT), None, "C1", "1.1")
    monkeypatch.setattr(runner, "_execute", fake_usage_limited_execute)
    result = await runner.run_model(config, request, "要点を抽出して")
    assert result.is_error and result.limit_reset_at is not None
    assert fake_codex_execute.call_count == 0

async def test_voice_handoff_sends_read_only_to_research_agent(config, store, fake_remote):
    await Handoff(config, store).ask("research", "要点は？", "vlm")
    assert remote.requests[-1]["read_only"] is True
```

- [ ] **Step 5: targeted tests を通す**

Run: `uv run --group dev --group agents pytest tests/test_runner.py tests/test_agent_claude.py tests/test_research_agent.py tests/test_voice.py -q`

Expected: PASS。

### Task 4: callers と音声 API を新しい public API に統一する

**Files:**
- Modify: `src/kei_agent/assistant.py`, `src/kei_agent/research.py`, `src/kei_agent/course.py`, `src/kei_agent/work.py`, `src/kei_agent/self_fix.py`
- Modify: `src/kei_agent_voice/handoff.py`, `src/kei_agent_voice/tools.py`
- Delete: `src/kei_agent_voice/think.py`
- Modify: `tests/conftest.py`, `tests/test_assistant.py`, `tests/test_voice.py`

**Interfaces:**
- Consumes: `runner.run_model(config, request, prompt, on_activity=None, on_text=None)` from Task 2。
- Produces: `Tools.call()` が公開する content lookup tool は `ask_agent` だけ。

- [ ] **Step 1: 旧 API が製品コードにないことを failing test にする**

```python
def test_voice_exposes_only_ask_agent():
    assert "ask_agent" in {tool["name"] for tool in DEFINITIONS}
    assert "ask_research" not in {tool["name"] for tool in DEFINITIONS}

def test_legacy_runner_and_voice_modules_are_removed():
    assert not Path("src/kei_agent_voice/think.py").exists()
    assert "def run_claude(" not in Path("src/kei_agent/runner.py").read_text()
```

- [ ] **Step 2: test が legacy compatibility により FAIL することを確認する**

Run: `uv run --group dev --group agents pytest tests/test_voice.py tests/test_runner.py -q`

Expected: `ask_research` と `think.py` が残るため FAIL。

- [ ] **Step 3: caller を `ExecutionRequest` へ移し compatibility code を削除する**

各 caller で `resolve_selected()` の直後に request を組み立て、`runner.run_model()` だけを呼ぶ。`Tools.call()` の `ask_research` alias と `Tools.ask_research()`、`think.py`、その直接テストを削除する。App Home の model/effort setter/getter/action と SQLite key を消し、provider 選択だけを残す。

- [ ] **Step 4: 全 caller を動かす test を追加する**

```python
async def test_detached_router_uses_router_recipe_not_research_profile(env):
    await scheduler.run_daily("2026-09-24")
    request = fake_runner.requests[-1]
    assert request.recipe.actor == "router"
    assert request.recipe.use_case is UseCase.OVERVIEW_DAILY
```

- [ ] **Step 5: public API suite を通す**

Run: `uv run --group dev --group agents pytest tests/test_assistant.py tests/test_voice.py tests/test_settings.py tests/test_home.py -q`

Expected: PASS。

### Task 5: documentation と全体検証を同期する

**Files:**
- Modify: `docs/design.md`, `docs/agents.md`, `docs/agents/orchestrator.md`, `docs/agents/research.md`, `docs/model-policy.md`, `docs/voice.md`
- Modify: `config.toml`

- [ ] **Step 1: documentation contract test を書く**

```python
def test_design_has_no_obsolete_fixed_codex_voice_or_gpt_5_6():
    text = Path("docs/design.md").read_text(encoding="utf-8")
    assert "gpt-5.6" not in text
    assert "固定 Codex" not in text
    assert "actor" in text and "read-only" in text
```

- [ ] **Step 2: test の失敗を確認する**

Run: `uv run --group dev --group agents pytest tests/test_docs_contract.py -q`

Expected: 古い設計記述により FAIL。ファイルがなければ同 test file を作成する。

- [ ] **Step 3: docs/config を正本に同期する**

`docs/model-policy.md` に actor × use case の recipe と manual 例外を記す。`docs/design.md` と agent docs から research 固定 runner、Codex 固定音声委譲、profile model override の説明を削る。`config.toml` は provider と connector だけを示す。

- [ ] **Step 4: legacy symbol scan を実行する**

Run: `rg -n 'ask_research|voice\.think|def run_claude\(|agent\.[^.]+\.(model|reasoning_effort)' src tests docs`

Expected: product code に該当なし。履歴を説明する spec は対象外として必要最小限に保つ。

- [ ] **Step 5: 全検証を実行する**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run --group dev --group agents pytest -q && UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uvx ruff check . && git diff --check`

Expected: すべて成功、`git diff --check` は出力なし。
