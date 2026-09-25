# Claude Code / Codex 共通実行契約 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code を振る舞いの基準として、Codex を選んでも同じ Kei Agent の指示・能力・会話・出力・上限管理で動かす。

**Architecture:** `ExecutionContract` が agent/use case、recipe、指示、skill、能力を確定し、Claude / Codex adapter がそれを各 runtime に変換する。session は provider と指示版に紐づけ、切替時は公開済み Slack 履歴から新規 session を作る。runner と A2A は共通結果を返し、Slack 表示と quota は provider 非依存の境界で扱う。

**Tech Stack:** Python 3.13 以上、SQLite、asyncio、Claude Code CLI、Codex CLI / app-server、pytest、ruff。

**Spec:** `docs/superpowers/specs/2026-09-24-provider-execution-parity-design.md`

## Global Constraints

- 既定 provider、provider 間の自動 fallback、model / effort の adapter 側上書きを作らない。
- Claude Code の既存の利用者向け挙動を基準にするが、権限を広げて「同等」とはみなさない。
- provider を切り替えたら、切替先に保存された session があっても Slack 履歴から新規 session を始める。
- 出力は検証済み final と固定進捗だけを Slack に出す。途中 text、tool 出力、秘密、内部エラーは出さない。
- 既存 session ID と利用者の作業を消さない。コミット・push・デプロイは明示依頼がない限り行わない。
- 必要能力を強制できない実行は起動前に fail closed する。

## Review Focus

- 旧 DB に provider 不明の session ID がある場合、誤再開も値の削除もしないことを Task 4 で確認する。
- Claude → Codex → Claude と戻った場合、古い Claude session へ途中の Codex 発話を飛ばさないことを Task 4 で確認する。
- Codex の CLI sandbox が local filesystem を制限しても、hosted app / MCP を暗黙に許可しないことを Task 3 で確認する。
- 成功 text を出した後に process が非ゼロ終了した場合、Slack に途中 text を出さないことを Task 5 で確認する。
- 片方の quota 中にもう片方の定期処理が動けることを Task 6 で確認する。

---

## File Structure

- Create: `src/kei_agent/execution_contract.py` — request から provider 非依存の指示・skill・能力契約を作る。
- Create: `src/kei_agent/provider_permissions.py` — Claude / Codex に必要な権限と実際に強制できる権限を比較する。
- Modify: `src/kei_agent/runner.py` — contract を受けて CLI command と `RunResult` を構築し、異常終了を正規化する。
- Modify: `src/kei_agent/codex_app_server.py`, `src/kei_agent_a2a/claude.py` — course / work の app-server adapter に同じ指示・能力を渡す。
- Modify: `src/kei_agent/store.py` — provider 別 session と quota 状態を永続化し、旧値を保持する migration。
- Modify: `src/kei_agent/assistant.py`, `src/kei_agent/course.py`, `src/kei_agent/research.py`, `src/kei_agent/work.py`, `src/kei_agent/schedule.py` — provider 切替、履歴引き継ぎ、quota を共通 API に移す。
- Modify: `src/kei_agent/response_output.py` — final-only の境界を runner / A2A の両方に適用する。
- Test: `tests/test_execution_contract.py`, `tests/test_provider_permissions.py`, `tests/test_store.py`, `tests/test_runner.py`, `tests/test_codex_app_server.py`, `tests/test_agent_claude.py`, `tests/test_assistant.py`, `tests/test_course_agent.py`, `tests/test_schedule.py`, `tests/test_response_output.py`。
- Modify: `docs/design.md`, `docs/agents.md` — 実装後の運用仕様と fail-closed 条件。

### Task 1: 実行契約と指示版

**Files:**
- Create: `src/kei_agent/execution_contract.py`
- Modify: `src/kei_agent/runner.py`
- Test: `tests/test_execution_contract.py`

**Interfaces:**
- Consumes: `ExecutionRequest` / `ResolvedModel` / `Workspace`。
- Produces: `ExecutionContract(recipe, prompt_text, prompt_version, skill_dir, capabilities, read_only)` と `resolve_contract(config, request) -> ExecutionContract`。

- [ ] **Step 1: 失敗する契約テストを書く**

```python
def test_contract_uses_agent_prompt_and_recipe(config, research_request):
    contract = resolve_contract(config, research_request)
    assert contract.recipe is research_request.recipe
    prompt_path = research_request.workspace.system_prompt or config.system_prompt_path
    assert contract.prompt_text == prompt_path.read_text()
    assert contract.skill_dir == config.agent_plugin_dir("research") / "skills"
    assert contract.prompt_version

def test_router_does_not_inherit_research_skills(config, router_request):
    assert resolve_contract(config, router_request).skill_dir is None
```

- [ ] **Step 2: red を確認する** — `uv run --group dev --group agents pytest tests/test_execution_contract.py -q` が未定義 API で失敗する。
- [ ] **Step 3: 最小実装を加える**

```python
@dataclass(frozen=True)
class ExecutionContract:
    recipe: ResolvedModel
    prompt_text: str
    prompt_version: str
    skill_dir: Path | None
    capabilities: frozenset[str]
    read_only: bool

def resolve_contract(config: Config, request: ExecutionRequest) -> ExecutionContract:
    prompt_path = request.workspace.system_prompt or config.system_prompt_path
    text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    skill_dir = (config.agent_plugin_dir(request.recipe.actor) / "skills"
                 if request.recipe.actor in {"research", "course", "work"} else None)
    return ExecutionContract(request.recipe, text, sha256(text.encode()).hexdigest()[:12],
                             skill_dir, required_capabilities(request), request.read_only)

def required_capabilities(request: ExecutionRequest) -> frozenset[str]:
    required = {"filesystem.read", "filesystem.deny_read", "network.domain_allowlist"}
    if not request.read_only:
        required.add("filesystem.write_scope")
    if request.recipe.actor == "research" and not request.read_only:
        required.add("mcp.allowlist")
    return frozenset(required)
```

`required_capabilities(request)` は `ExecutionRequest` の actor、workspace、read-only から読み取り・書き込み範囲、domain allowlist、必要な connector 名を導出する。この関数を Task 3 の権限検査に使う。Claude command の既存 prompt / plugin 選択は契約から得る。

- [ ] **Step 4: green と回帰を確認する** — `uv run --group dev --group agents pytest tests/test_execution_contract.py tests/test_runner.py -q` を通す。

### Task 2: Codex への同じ指示・skill 配布

**Files:**
- Modify: `src/kei_agent/runner.py`, `src/kei_agent/codex_app_server.py`, `src/kei_agent_a2a/claude.py`
- Modify: `src/kei_agent_course/executor.py`, `src/kei_agent_work/connector.py`
- Test: `tests/test_runner.py`, `tests/test_codex_app_server.py`, `tests/test_agent_claude.py`

**Interfaces:**
- Consumes: `ExecutionContract` from Task 1。
- Produces: `codex_instruction_config(contract) -> list[str]`、`install_agent_skills(contract, cwd) -> None`、`build_app_request(contract, prompt) -> AppRequest`（`instructions` と `input` を分離）。

- [ ] **Step 1: command と app-server の failing test を書く**

```python
def test_codex_command_includes_canonical_instruction_and_agent_skills(config, research_request):
    command = build_command(config, research_request)
    assert any(part.startswith("developer_instructions=") for part in command)
    assert "research" in resolve_contract(config, research_request).skill_dir.parts

def test_course_app_request_receives_course_guide_only(course_contract, app_request):
    request = build_app_request(course_contract, app_request.prompt)
    assert request.instructions == course_contract.prompt_text
    assert request.input == app_request.prompt
```

- [ ] **Step 2: red を確認する** — `uv run --group dev --group agents pytest tests/test_runner.py tests/test_codex_app_server.py tests/test_agent_claude.py -q` の追加 test が失敗する。
- [ ] **Step 3: Codex adapter を実装する**

```python
def codex_instruction_config(contract: ExecutionContract) -> list[str]:
    return ["--config", "developer_instructions=" + json.dumps(contract.prompt_text, ensure_ascii=False)]

def install_agent_skills(contract: ExecutionContract, cwd: Path) -> None:
    if contract.skill_dir is None:
        return
    target_root = cwd / ".agents" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    # `.kei-agent-managed-skills.json` に記録したリンクだけを検査・更新する。
    # 利用者が作った同名 skill は上書きせず、衝突なら fail closed にする。
    for source in contract.skill_dir.iterdir():
        if source.is_dir() and (source / "SKILL.md").is_file():
            target = target_root / source.name
            if not target.exists() and not target.is_symlink():
                target.symlink_to(Path(os.path.relpath(source, target_root)))

@dataclass(frozen=True)
class AppRequest:
    instructions: str
    input: str

def build_app_request(contract: ExecutionContract, prompt: str) -> AppRequest:
    return AppRequest(instructions=contract.prompt_text, input=prompt)
```

`install_agent_skills` は作業場で過去に公開した Kei Agent 管理リンクを manifest から特定し、対象 actor 以外の管理リンクを取り除いてから現在のリンクを作る。利用者が所有する `.agents/skills` は消さず、同名衝突では fail closed とする。app-server の connector 契約には `app.allowlist` を追加する（通常の course / work CLI 実行には追加しない）。app-server では同じ guide を開発者指示として渡せるか実 runtime の request 仕様を確認し、渡せない場合は `turn/start` の入力で agent 指示と質問を分離する。分離も保証できなければ fail closed とする。course / work の既存 user-prompt 前置きは共通化後に削除し、JSON 専用 request の形式指定は保持する。

- [ ] **Step 4: green と回帰を確認する** — 同じ test 群を通し、研究 skill が course / work に現れない test を追加する。

### Task 3: 権限 preflight と fail closed

**Files:**
- Create: `src/kei_agent/provider_permissions.py`
- Modify: `src/kei_agent/runner.py`, `src/kei_agent/codex_app_server.py`
- Test: `tests/test_provider_permissions.py`, `tests/test_runner.py`, `tests/test_codex_app_server.py`

**Interfaces:**
- Consumes: `ExecutionContract.capabilities` from Task 1。
- Produces: `preflight(config, contract, runtime: Literal["claude_cli", "codex_cli", "codex_app"]) -> PermissionProfile`、`CapabilityUnavailable`。

- [ ] **Step 1: 不足能力で止まる failing test を書く**

```python
@pytest.mark.parametrize("missing", ["filesystem.deny_read", "filesystem.write_scope",
                                     "network.domain_allowlist", "app.allowlist"])
def test_codex_preflight_fails_closed(config, codex_contract, missing):
    with pytest.raises(CapabilityUnavailable):
        preflight(config, codex_contract, "codex_cli", unavailable={missing})

def test_read_only_contract_never_grants_write(config, readonly_contract):
    assert "filesystem.write" not in preflight(config, readonly_contract, "claude_cli").grants
```

- [ ] **Step 2: red を確認する** — `uv run --group dev --group agents pytest tests/test_provider_permissions.py -q` が失敗する。
- [ ] **Step 3: 権限対応表と runtime 検査を実装する**

```python
class CapabilityUnavailable(RuntimeError):
    pass

def preflight(config, contract, runtime, *, unavailable=frozenset()):
    required = contract.capabilities
    enforceable = inspect_runtime_permissions(config, runtime)
    missing = required - enforceable - set(unavailable)
    if missing or (required & set(unavailable)):
        raise CapabilityUnavailable(",".join(sorted(missing | (required & set(unavailable)))))
    return build_profile(config, contract, runtime)
```

`inspect_runtime_permissions` は CLI の read deny / write scope / domain proxy と、app / MCP の個別 allowlist を別々に扱う。Codex 設定をコマンドへ渡す前に現在の CLI 版・設定で本当に強制されることを確認する。単に `--sandbox workspace-write` があるだけでは合格しない。強制可能性が不明なら失敗を返す。

- [ ] **Step 4: green と権限回帰を確認する** — `uv run --group dev --group agents pytest tests/test_provider_permissions.py tests/test_runner.py tests/test_codex_app_server.py -q` を通す。

### Task 4: provider 別 session と Slack 履歴引き継ぎ

**Files:**
- Modify: `src/kei_agent/store.py`, `src/kei_agent/assistant.py`, `src/kei_agent/course.py`, `src/kei_agent/research.py`, `src/kei_agent/work.py`
- Test: `tests/test_assistant.py`; Create: `tests/test_course_agent.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `Store.session_for(channel, thread_ts, agent, provider, prompt_version) -> str | None`、`Store.set_session(..., session_id, prompt_version) -> None`、`Store.last_provider(...) -> str | None`。

- [ ] **Step 1: migration / 切替の failing test を書く**

```python
def test_legacy_session_is_preserved_but_not_resumed(store):
    store.upsert_thread("C", "1", "research", "old-id")
    assert store.session_for("C", "1", "research", "codex", "v2") is None
    assert store.get_thread("C", "1")["session_id"] == "old-id"

@pytest.mark.parametrize("sequence", [("claude", "codex"), ("codex", "claude")])
async def test_provider_switch_starts_from_slack_history(assistant, sequence):
    await run_two_turns(assistant, sequence)
    assert assistant.last_run.session_id is None
    assert "会話履歴" in assistant.last_run.prompt
```

- [ ] **Step 2: red を確認する** — `uv run --group dev --group agents pytest tests/test_assistant.py tests/test_store.py -q` で追加 test が失敗する。
- [ ] **Step 3: session table と選択規則を実装する**

```sql
CREATE TABLE IF NOT EXISTS provider_sessions (
  channel TEXT NOT NULL, thread_ts TEXT NOT NULL, agent TEXT NOT NULL,
  provider TEXT NOT NULL, session_id TEXT NOT NULL, prompt_version TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (channel, thread_ts, agent, provider)
);
```

`threads.session_id` と旧 `agent_sessions.session_id` は legacy 値として残す。`_converse` と A2A では `last_provider` と対象 provider が違う、または指示版が違う場合、保存 session を使わず `thread_messages` → `history_prompt` を渡す。履歴は Slack 公開本文のみ、既存の件数上限と dropped 表示を維持する。成功後のみ新 session と最後の provider を保存する。同 provider / 同指示版は再開する。

- [ ] **Step 4: green と往復・再起動回帰を確認する** — `uv run --group dev --group agents pytest tests/test_assistant.py tests/test_course_agent.py tests/test_store.py -q` を通す。

### Task 5: final-only と失敗の正規化

**Files:**
- Modify: `src/kei_agent/runner.py`, `src/kei_agent/codex_app_server.py`, `src/kei_agent_a2a/claude.py`, `src/kei_agent/assistant.py`, `src/kei_agent/response_output.py`
- Test: `tests/test_runner.py`, `tests/test_codex_app_server.py`, `tests/test_assistant.py`, `tests/test_response_output.py`

**Interfaces:**
- Produces: `RunResult.failure_kind: Literal["quota", "timeout", "session_missing", "capability", "runtime"] | None`、`finalize_run_result(result: RunResult, returncode: int) -> RunResult`。

- [ ] **Step 1: 途中出力と異常終了の failing test を書く**

```python
def test_codex_intermediate_message_is_not_final():
    result = RunResult()
    apply_codex_event(result, {"type": "item.completed", "item": {"type": "agent_message", "text": "調査中"}})
    assert result.text == ""

def test_nonzero_exit_with_text_is_failure():
    result = finalize_run_result(RunResult(text="途中結果"), returncode=1)
    assert result.is_error
    assert result.failure_kind == "runtime"
```

- [ ] **Step 2: red を確認する** — `uv run --group dev --group agents pytest tests/test_runner.py tests/test_codex_app_server.py tests/test_response_output.py -q` で追加 test が失敗する。
- [ ] **Step 3: final と failure を分離する**

```python
def public_text(result: RunResult) -> str:
    if result.is_error:
        return safe_failure(result.failure_kind)
    return finalize_conversation(result.text)
```

stream `agent_message` は最終 turn 完了と区別し、最後の候補を `turn.completed` まで保留する。未確定 text は `RunResult.text` と `on_text` に流さない。CLI 非ゼロ、timeout、出力欠落を必ず `is_error=True` にする。A2A の構造化結果は raw に保持しつつ、Slack に出す free-form 文だけ共通 renderer を通す。既存 Daily / Retro 契約と固定進捗を回帰確認する。

- [ ] **Step 4: green を確認する** — `uv run --group dev --group agents pytest tests/test_runner.py tests/test_codex_app_server.py tests/test_assistant.py tests/test_response_output.py -q` を通す。

### Task 6: provider 別 quota と定期処理

**Files:**
- Modify: `src/kei_agent/store.py`, `src/kei_agent/assistant.py`, `src/kei_agent/schedule.py`
- Test: `tests/test_assistant.py`, `tests/test_schedule.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `Store.limit_until(provider) -> float`、`Store.set_limit_until(provider, until) -> None`、`Scheduler.can_run(name, now, provider) -> bool`。

- [ ] **Step 1: provider 分離の failing test を書く**

```python
def test_claude_limit_does_not_stop_codex_schedule(store, schedule, now):
    store.set_limit_until("claude", now + 3600)
    assert schedule.can_run("daily", now, "codex")

def test_limit_survives_restart(store_path, now):
    first = Store(store_path)
    first.set_limit_until("codex", now + 3600)
    first.conn.close()
    assert Store(store_path).limit_until("codex") == now + 3600
```

- [ ] **Step 2: red を確認する** — `uv run --group dev --group agents pytest tests/test_schedule.py tests/test_assistant.py tests/test_store.py -q` の追加 test が失敗する。
- [ ] **Step 3: provider ごとの上限を永続化する**

```sql
CREATE TABLE IF NOT EXISTS provider_limits (
  provider TEXT PRIMARY KEY, until REAL NOT NULL
);
```

`note_limit` / `defer_for_limit` は実行した provider を必須引数にし、ユーザー文言を provider 名付きの固定文へ変える。schedule の `tick` / `run_or_defer` はそのタスクで解決した provider だけを調べる。reset 不明時は provider ごとの fallback 時刻で再試行し、他方へ切り替えない。既存の遅延処理 payload に provider がない場合は、その時点の明示設定で解決し、既定 provider を推測しない。

- [ ] **Step 4: green と全体回帰を確認する** — `uv run --group dev --group agents pytest tests/test_schedule.py tests/test_assistant.py tests/test_store.py -q` を通す。

### Task 7: 実 runtime canary と文書化

**Files:**
- Modify: `docs/design.md`, `docs/agents.md`
- Test: `tests/test_execution_contract.py`, `tests/test_provider_permissions.py`

**Interfaces:**
- Consumes: Tasks 1–6 の contract、preflight、session、結果分類。
- Produces: 運用上の provider parity 対応表と検証結果。

- [ ] **Step 1: matrix test を追加する** — router / research / course / work の各 provider について、解決済み model / effort、prompt version、skill、必要能力を parametrized test で比較する。`uv run --group dev --group agents pytest tests/test_execution_contract.py tests/test_provider_permissions.py -q` を実行する。
- [ ] **Step 2: read-only canary を実行する** — 使用可能な実 Claude / Codex で、外部書き込みをしない分類・指示・skill 発見・session 切替を検証する。接続・ログイン・能力強制が欠ける場合は、その provider / 経路を未検証として記録し、成功と報告しない。
- [ ] **Step 3: 全体を検証する** — `uv run --group dev --group agents pytest -q`、`uvx ruff check .`、`git diff --check` の出力を確認する。
- [ ] **Step 4: 文書を実装に合わせる** — `docs/design.md` と `docs/agents.md` に provider 切替、旧 session、fail-closed、quota の仕様と実 runtime canary の結果を書く。未検証の能力を「対応済み」と書かない。
