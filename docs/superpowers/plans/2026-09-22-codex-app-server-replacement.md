# Codex App Server Complete Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Codex App Server と接続済みの Box / Notion / Outlook を使い、KeiAgent の agent ごとの provider 切替を実働させる。

**Architecture:** App Server JSON-RPC は codex_app_server に閉じ込め、各依頼を一時子プロセス・一時設定で実行する。まず読み取り専用の discovery process でアカウント固有の App ID を解決し、許可した ID だけを有効にした run process を別に起動する。connector 許可は agent policy から導き、callable を実行前に検査する。研究の workspace runner と scoped Notion gateway は維持し、大学・仕事の自由質問を provider-aware connector に置き換える。

**Tech Stack:** Python 3.12、asyncio、Codex CLI App Server（stdio JSON-RPC）、pytest、Slack Bolt Block Kit、SQLite。

**Spec:** docs/superpowers/specs/2026-09-22-codex-app-server-replacement-design.md

## Global Constraints

- Box は大学アカウントでログイン済みの接続だけを使用し、Custom App、OAuth、token export は行わない。
- course は Box read-only、Notion は大学ホーム配下だけ。work は Outlook read-only。SharePoint と Teams は有効化しない。
- research は research-notion gateway と workspace-write を使用し、広い Notion App connector を使用しない。W&B は read-first。
- 未接続・未許可・非 callable は ok: false で終え、Claude に自動 fallback しない。
- logs / hooks は agent、provider、model、duration、success、opaque thread id のみを扱う。

---

## File Structure

- src/kei_agent/agent_policy.py: agent と App の許可境界。
- src/kei_agent/codex_app_server.py: App Server JSON-RPC と RunResult への変換。
- src/kei_agent/settings.py: App Home の profile override。
- src/kei_agent/home.py, settings_actions.py: provider/model/effort/readiness UI。
- src/kei_agent_a2a/claude.py: provider-aware connector facade（既存 import 名を維持）。
- src/kei_agent/runner.py: research の scoped gateway config。

### Task 1: Agent policy と App Home profile override

**Files:**

- Create: src/kei_agent/agent_policy.py
- Modify: src/kei_agent/config.py
- Modify: src/kei_agent/settings.py
- Test: tests/test_themes.py, tests/test_settings.py

**Interfaces:** Produces AppPolicy(agent: str, app_names: frozenset[str], read_only: bool), policy_for(agent: str) -> AppPolicy, settings.set_agent_profile(...), and settings.agent_profile(config, store, agent) -> AgentProfile. Account-specific App IDs are resolved only at runtime.

- [ ] **Step 1: Write failing tests**

~~~python
def test_course_policy_allows_only_box_and_notion():
    assert policy_for("course").app_names == frozenset({"Box", "Notion"})

def test_home_override_changes_only_named_agent(config, store):
    settings.set_agent_profile(store, "course", "codex", "gpt-5.6-terra", "high")
    assert settings.agent_profile(config, store, "course").provider == "codex"
    assert settings.agent_profile(config, store, "work").provider == "claude"
~~~

- [ ] **Step 2: Verify RED**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_themes.py tests/test_settings.py -q

Expected: FAIL because policy and profile override functions do not exist.

- [ ] **Step 3: Implement minimal policy and storage**

~~~python
POLICIES = {
    "course": AppPolicy("course", frozenset({"Box", "Notion"}), False),
    "work": AppPolicy("work", frozenset({"Microsoft Outlook Email", "Microsoft Outlook Calendar"}), True),
    "research": AppPolicy("research", frozenset(), False),
}
~~~

Save provider/model/reasoning-effort in SQLite as agent.<agent>.<field>. Validate agent, claude|codex, model length 120, and effort low|medium|high|xhigh. Connectors remain policy-owned and are never user-editable.

- [ ] **Step 4: Verify GREEN and commit**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_themes.py tests/test_settings.py -q

Expected: PASS.

~~~bash
git add src/kei_agent/agent_policy.py src/kei_agent/config.py src/kei_agent/settings.py tests/test_themes.py tests/test_settings.py
git commit -m "エージェント別Codex権限と設定上書きを追加する"
~~~

### Task 2: App Server adapter と readiness preflight

**Files:**

- Create: src/kei_agent/codex_app_server.py
- Create: tests/test_codex_app_server.py
- Modify: src/kei_agent/run_hooks.py, tests/test_run_hooks.py

**Interfaces:** Produces AppServerClient.run(prompt, policy, model, reasoning_effort, on_activity, on_text) -> RunResult and AppServerUnavailable(RuntimeError).

- [ ] **Step 1: Write failing protocol tests**

~~~python
async def test_run_rejects_non_callable_box(fake_server):
    fake_server.reply_installed({"box": (True, False)})
    with pytest.raises(AppServerUnavailable, match="Box"):
        await client.run("資料を探して", policy_for("course"), "", "high", None, None)

async def test_run_mentions_only_allowed_apps(fake_server):
    result = await client.run("予定を教えて", policy_for("work"), "", "high", None, None)
    assert fake_server.mentions == ["app://outlook_email", "app://outlook_calendar"]
    assert result.text == "予定を確認しました"
~~~

- [ ] **Step 2: Verify RED**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_codex_app_server.py -q

Expected: FAIL because the adapter does not exist.

- [ ] **Step 3: Implement minimal adapter**

Start a discovery codex app-server --stdio process and send initialize(experimentalApi=true), initialized, then app/installed(forceRefresh=true). Resolve policy display names to its current internal IDs and reject a missing, disabled, or non-callable App. Start a separate run process with process-only -c overrides: apps._default.enabled=false plus the resolved IDs. Send thread/start, then turn/start with one text input and one app:// mention per allowed resolved App.

Map thread.started to opaque session id, completed agent_message to RunResult.text, and failed turns to generic errors. Never log JSON-RPC request / response contents. Change CONNECTOR_POLICY to course {box, notion}, work {outlook_email, outlook_calendar}, research {research-notion, wandb}.

- [ ] **Step 4: Verify GREEN and commit**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_codex_app_server.py tests/test_run_hooks.py -q

Expected: PASS.

~~~bash
git add src/kei_agent/codex_app_server.py src/kei_agent/run_hooks.py tests/test_codex_app_server.py tests/test_run_hooks.py
git commit -m "Codex App Server実行器を追加する"
~~~

### Task 3: course / work connector provider dispatch

**Files:**

- Modify: src/kei_agent_a2a/claude.py
- Modify: src/kei_agent_course/executor.py
- Modify: src/kei_agent_work/connector.py
- Test: tests/test_course_agent.py, tests/test_work_agent.py

**Interfaces:** Changes ask_connector to ask_connector(config, store, agent, prompt, allowed, plugin_dir, deny=(), timeout_minutes=3) -> str.

- [ ] **Step 1: Write failing dispatch tests**

~~~python
async def test_course_uses_codex_when_selected(config, store, monkeypatch):
    settings.set_agent_profile(store, "course", "codex", "", "high")
    monkeypatch.setattr(claude, "ask_codex_app", AsyncMock(return_value="Boxを確認しました"))
    assert await claude.ask_connector(config, store, "course", "資料は？", (), Path(".")) == "Boxを確認しました"

async def test_work_codex_failure_never_falls_back_to_claude(config, store, monkeypatch):
    settings.set_agent_profile(store, "work", "codex", "", "high")
    monkeypatch.setattr(claude, "ask_codex_app", AsyncMock(side_effect=claude.ConnectorError("Outlook が未接続")))
    with pytest.raises(claude.ConnectorError, match="Outlook"):
        await claude.ask_connector(config, store, "work", "会議は？", (), Path("."))
~~~

- [ ] **Step 2: Verify RED**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_course_agent.py tests/test_work_agent.py -q

Expected: FAIL because callers do not supply store / agent.

- [ ] **Step 3: Implement facade and callers**

Use the effective stored profile. Keep the existing Claude command unchanged for claude; use AppServerClient and policy_for(agent) for codex. Convert only generic readiness/execution failures to ConnectorError; never start Claude after Codex fails. Keep Moodle/ICS/notion_sync direct handlers intact. Thread Store through course and work call paths.

- [ ] **Step 4: Verify GREEN and commit**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_course_agent.py tests/test_work_agent.py -q

Expected: PASS.

~~~bash
git add src/kei_agent_a2a/claude.py src/kei_agent_course/executor.py src/kei_agent_work/connector.py tests/test_course_agent.py tests/test_work_agent.py
git commit -m "大学と仕事のconnectorをCodex切替対応にする"
~~~

### Task 4: research Codex を scoped gateway に固定する

**Files:**

- Modify: src/kei_agent/runner.py, src/kei_agent/codex_runtime.py
- Test: tests/test_runner.py, tests/test_codex_runtime.py
- Modify: docs/agents.md

**Interfaces:** Produces write_codex_project_config(config, cwd) -> Path, containing only mcp_servers.research-notion, gateway URL, env_http_headers Authorization = KEI_AGENT_NOTION_GATEWAY_AUTH, and enabled = true.

- [ ] **Step 1: Write failing gateway test**

~~~python
def test_research_codex_config_contains_only_scoped_gateway(config, tmp_path):
    text = runner.write_codex_project_config(config, tmp_path).read_text()
    assert "[mcp_servers.research-notion]" in text
    assert config.notion_gateway_url in text
    assert "NOTION_TOKEN" not in text
~~~

- [ ] **Step 2: Verify RED**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_runner.py tests/test_codex_runtime.py -q

Expected: FAIL because the writer is absent.

- [ ] **Step 3: Implement the config lifecycle**

Create the project config beneath the theme workspace, place the bearer only in an environment variable, and remove the generated config in finally. Retain skill symlinks, workspace-write, timeouts, hooks, and JSONL conversion. A declared W&B connector that is not ready returns 未接続; do not invoke W&B CLI/SDK or search credentials.

- [ ] **Step 4: Verify GREEN and commit**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_runner.py tests/test_codex_runtime.py tests/test_run_hooks.py -q

Expected: PASS.

~~~bash
git add src/kei_agent/runner.py src/kei_agent/codex_runtime.py tests/test_runner.py tests/test_codex_runtime.py docs/agents.md
git commit -m "研究CodexをNotion gatewayに限定する"
~~~

### Task 5: Slack Home controls、regression、deployment

**Files:**

- Modify: src/kei_agent/home.py, src/kei_agent/settings_actions.py
- Test: tests/test_home.py
- Modify: docs/agents.md, deploy/README.md, README.md

- [ ] **Step 1: Write failing Home test**

~~~python
def test_home_shows_course_provider_and_boundaries(config, store):
    text = _texts(home.build_home(config, store, [], True, {"course": {"box": True, "notion": True}}))
    assert "大学: Codex / Claude" in text
    assert "Box: 読み取りのみ" in text
    assert "Notion: 大学ホーム内で操作" in text
~~~

- [ ] **Step 2: Verify RED, implement, and verify GREEN**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_home.py -q

Expected: FAIL because no agent controls exist.

Add static selectors for provider, model, and effort for research/course/work. Display only readiness and permission summaries; never account ids, app ids, tool lists, data, or tokens. Disable Codex selection unless every policy App is callable. Validate actions and republish Home.

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_home.py tests/test_settings.py -q

Expected: PASS.

- [ ] **Step 3: Run complete verification**

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest -q

Run: UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run python -m compileall -q src

Run: git diff --check

Expected: all exit 0. If sandbox blocks test sockets, use the already-approved elevated test invocation.

- [ ] **Step 4: Run safe App readiness probe and human-confirmed smoke tests**

Call only app/installed(forceRefresh=true) and normalize {name, enabled, callable} for Box, Notion, Outlook Email, Outlook Calendar. Then ask the user to send one harmless Slack course Box-read request and one Outlook-read request. Do not use grades, Box file bodies, email bodies, or calendar details as test data.

- [ ] **Step 5: Document, commit, integrate, and deploy**

Document selection, readiness errors, no fallback, and SharePoint/Teams exclusion. Commit the Home and documentation changes, merge reviewed work to main, push, run the deployment installer from the main checkout, and verify every com.kei-agent.* service is running.

~~~bash
git add src/kei_agent/home.py src/kei_agent/settings_actions.py tests/test_home.py docs/agents.md deploy/README.md README.md
git commit -m "App Homeにagent別Codex設定を追加する"
~~~
