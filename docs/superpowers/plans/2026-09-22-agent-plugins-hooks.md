# Agent Plugins and Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 研究・大学・仕事へ隔離された専用skillと安全hookを配布し、各Claudeが担当pluginだけを使うようにする。

**Architecture:** 3つのClaude Code pluginを `plugin/<agent>/` に置き、研究runnerと大学・仕事connectorが担当directoryだけを `--plugin-dir` へ渡す。外部toolは既存allowlistを第一防御、plugin同梱PreToolUse hookを第二防御にする。

**Tech Stack:** Python 3.13、Claude Code plugins、SKILL.md、hooks.json、pytest、Ruff

**Spec:** `docs/superpowers/specs/2026-09-22-agent-skills-hooks-design.md`

## Global Constraints

- 同じClaudeプロセスへ担当外pluginを複数ロードしない。
- 研究Notionは `research-notion` MCPだけを使い、生の `NOTION_TOKEN` を渡さない。
- 大学は授業ホーム配下のNotion全操作可、Boxは読み取り専用。
- 仕事のMicrosoft 365は読み取り専用。
- promptは常時規則、skillは依頼別の反復手順に分ける。
- コミットとpushは依頼されるまで行わない。

---

### Task 1: 研究pluginの移行

**Files:**
- Move: `plugin/.claude-plugin/plugin.json` → `plugin/research/.claude-plugin/plugin.json`
- Move: `plugin/skills/job/` → `plugin/research/skills/running-jobs/`
- Move: `plugin/skills/literature/` → `plugin/research/skills/researching-literature/`
- Create: `tests/test_plugins.py`

**Interfaces:**
- Produces: valid research plugin with skills `running-jobs`, `researching-literature`

- [ ] **Step 1: plugin構造とfrontmatterテストを書く**

```python
def test_research_plugin_has_renamed_skills():
    skills = skill_metadata(Path("plugin/research"))
    assert set(skills) >= {"running-jobs", "researching-literature"}
    assert not Path("plugin/skills").exists()
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py::test_research_plugin_has_renamed_skills -q`

Expected: 新directoryがなくFAIL。

- [ ] **Step 3: ファイルを移して参照を更新する**

frontmatter名を `running-jobs` / `researching-literature` にし、script呼び出しは
`$PLUGIN_ROOT/skills/<name>/scripts/...` を使う。環境変数 `KEI_AGENT_PLUGIN_DIR` への依存を除く。

- [ ] **Step 4: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py::test_research_plugin_has_renamed_skills tests/test_jobs.py -q`

Expected: PASS。

---

### Task 2: agent別plugin routing

**Files:**
- Modify: `src/kei_agent/config.py`
- Modify: `src/kei_agent/runner.py`
- Modify: `src/kei_agent_a2a/claude.py`
- Modify: `src/kei_agent_course/connector.py`
- Modify: `src/kei_agent_work/connector.py`
- Test: `tests/test_runner.py`
- Test: `tests/test_agent_claude.py`

**Interfaces:**
- Produces: `Config.agent_plugin_dir(agent: str) -> Path`
- Changes: `ask_connector(..., plugin_dir: Path, ...) -> str`

- [ ] **Step 1: command隔離テストを書く**

```python
def test_research_runner_loads_only_research_plugin(config, workspace):
    command = runner.build_command(config, workspace, None)
    assert command[command.index("--plugin-dir") + 1].endswith("plugin/research")

def test_connector_loads_its_plugin(config):
    command = claude.connector_command(config, ["mcp__read"], (), config.agent_plugin_dir("course"))
    assert command[command.index("--plugin-dir") + 1].endswith("plugin/course")
    assert "Skill" in command
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_runner.py -k plugin tests/test_agent_claude.py -k plugin -q`

Expected: API不足でFAIL。

- [ ] **Step 3: routingを実装する**

```python
def agent_plugin_dir(self, agent: str) -> Path:
    if agent not in {"research", "course", "work"}:
        raise ValueError(f"未知のagent: {agent}")
    return self.repo_root / "plugin" / agent
```

研究runnerは常に `research` を使う。`ask_connector` は `--plugin-dir <path>` を加え、allowed toolsへ
`Skill` を加える。course/work connectorはそれぞれ明示directoryを渡す。

- [ ] **Step 4: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_runner.py tests/test_agent_claude.py tests/test_a2a.py tests/test_work_agent.py -q`

Expected: PASS。

---

### Task 3: 大学pluginとNotion全操作

**Files:**
- Create: `plugin/course/.claude-plugin/plugin.json`
- Create: `plugin/course/skills/finding-course-materials/SKILL.md`
- Create: `plugin/course/skills/managing-assignments/SKILL.md`
- Create: `plugin/course/skills/managing-course-notion/SKILL.md`
- Modify: `src/kei_agent_course/tools.py`
- Modify: `prompts/course.md`
- Test: `tests/test_plugins.py`
- Test: `tests/test_a2a.py`

**Interfaces:**
- Produces: three course skills
- Produces: `ALLOWED` containing every available Notion connector operation and read-only Box operations

- [ ] **Step 1: skill discoveryと権限テストを書く**

```python
def test_course_plugin_has_three_scoped_skills():
    assert set(skill_metadata(Path("plugin/course"))) == {
        "finding-course-materials", "managing-assignments", "managing-course-notion"}

def test_course_allows_notion_writes_but_not_box_writes():
    assert "mcp__claude_ai_Notion__notion-create-database" in tools.ALLOWED
    assert "mcp__claude_ai_Notion__notion-move-pages" in tools.ALLOWED
    assert not any(name.endswith("upload_file") for name in tools.ALLOWED)
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py -k course tests/test_a2a.py -k notion -q`

Expected: skills/toolがなくFAIL。

- [ ] **Step 3: 3つのSKILL.mdを作る**

各descriptionは次の使用条件で始める。

```yaml
description: Use when授業資料、学部要項、過去問をBoxから探す必要があるとき。
description: Use when課題の締切、内容、提出状態を確認または更新するとき。
description: Use when授業ホーム内のNotionページやデータベースを作成、整理、移動、複製、削除するとき。
```

本文には「授業ホーム外は操作しない」「Boxは読むだけ」「原典URLを返す」を該当skillへ一度だけ置く。

- [ ] **Step 4: Notion allowlistを全操作へ広げる**

現在 `DENY` にあるNotion操作を除き、接続中プロファイルが公開する `mcp__claude_ai_Notion__*` のうち、
検索、取得、query、作成、更新、移動、複製、削除、DB作成、comment/session操作を明示的に `ALLOWED` へ列挙する。
Boxのwrite toolは `DENY` に残す。

- [ ] **Step 5: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py -k course tests/test_a2a.py -q`

Expected: PASS。

---

### Task 4: 仕事plugin

**Files:**
- Create: `plugin/work/.claude-plugin/plugin.json`
- Create: `plugin/work/skills/researching-work-context/SKILL.md`
- Create: `plugin/work/skills/preparing-meetings/SKILL.md`
- Create: `plugin/work/skills/drafting-work-actions/SKILL.md`
- Modify: `prompts/work.md`
- Test: `tests/test_plugins.py`
- Test: `tests/test_work_agent.py`

**Interfaces:**
- Produces: three work skills; external tool allowlist remains read-only

- [ ] **Step 1: work skillとread-onlyテストを書く**

```python
def test_work_plugin_has_three_scoped_skills():
    assert set(skill_metadata(Path("plugin/work"))) == {
        "researching-work-context", "preparing-meetings", "drafting-work-actions"}

def test_work_connector_has_no_write_tools():
    forbidden = ("send", "create", "update", "delete", "move", "upload", "post")
    assert not any(any(word in name.lower() for word in forbidden) for name in connector.ALLOWED_ASK)
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py -k work tests/test_work_agent.py -q`

Expected: pluginがなくFAIL。

- [ ] **Step 3: skillを作りprompt重複を削る**

`drafting-work-actions` は文章案だけを返し、送信toolを呼ばない。`researching-work-context` は原典URLと
差出人・日付を示す。`preparing-meetings` は予定、参加者、関連メール、資料を集める。

- [ ] **Step 4: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py -k work tests/test_work_agent.py -q`

Expected: PASS。

---

### Task 5: 研究Notion skillとMCP routing

**Files:**
- Create: `plugin/research/skills/managing-research-notion/SKILL.md`
- Modify: `src/kei_agent/runner.py`
- Modify: `src/kei_agent/config.py`
- Modify: `src/kei_agent/guard.py`
- Test: `tests/test_plugins.py`
- Test: `tests/test_runner.py`
- Test: `tests/test_guard.py`

**Interfaces:**
- Consumes: `http://127.0.0.1:8791/mcp` from gateway plan
- Produces: research-only `--mcp-config` and `--strict-mcp-config`

- [ ] **Step 1: MCP設定とsecret分離テストを書く**

```python
def test_research_runner_uses_only_scoped_notion_mcp(config, workspace):
    command = runner.build_command(config, workspace, None)
    mcp = json.loads(command[command.index("--mcp-config") + 1])
    assert mcp["mcpServers"]["research-notion"]["url"] == "http://127.0.0.1:8791/mcp"
    assert "--strict-mcp-config" in command

def test_research_env_has_gateway_token_not_notion_token(config):
    env = runner.build_env(config, {"NOTION_TOKEN": "raw", "KEI_AGENT_NOTION_GATEWAY_TOKEN": "scoped"}, "c", "t")
    assert "NOTION_TOKEN" not in env
    assert env["KEI_AGENT_NOTION_GATEWAY_TOKEN"] == "scoped"
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_runner.py -k notion_mcp tests/test_guard.py -k gateway -q`

Expected: MCP configがなくFAIL。

- [ ] **Step 3: research-only MCP configを実装する**

```python
{"mcpServers": {"research-notion": {
    "type": "http",
    "url": config.notion_gateway_url,
    "headers": {"Authorization": "Bearer ${KEI_AGENT_NOTION_GATEWAY_TOKEN}"},
}}}
```

`guard.strip_env` は生Notion tokenを落とし、gateway tokenだけを明示的に戻す。skillはgateway停止時に
別Notion connectorやtokenを探さず、接続失敗を返す。

- [ ] **Step 4: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py -k research tests/test_runner.py tests/test_guard.py -q`

Expected: PASS。

---

### Task 6: plugin別PreToolUse hooks

**Files:**
- Create: `plugin/research/hooks/hooks.json`
- Create: `plugin/research/hooks/policy.py`
- Create: `plugin/course/hooks/hooks.json`
- Create: `plugin/course/hooks/policy.py`
- Create: `plugin/work/hooks/hooks.json`
- Create: `plugin/work/hooks/policy.py`
- Test: `tests/test_plugin_hooks.py`

**Interfaces:**
- Produces: each `policy.py` reads one hook event from stdin and exits `0` allow / `2` deny

- [ ] **Step 1: policy matrix testsを書く**

```python
@pytest.mark.parametrize(("agent","tool","allowed"), [
    ("course", "mcp__claude_ai_Box__get_file_content", True),
    ("course", "mcp__claude_ai_Box__upload_file", False),
    ("course", "mcp__claude_ai_Notion__notion-move-pages", True),
    ("work", "mcp__claude_ai_Outlook__search_mail", True),
    ("work", "mcp__claude_ai_Outlook__send_mail", False),
    ("research", "mcp__research-notion__create_page", True),
    ("research", "mcp__claude_ai_Notion__notion-search", False),
])
def test_hook_policy(agent, tool, allowed):
    result = run_policy(agent, {"tool_name": tool, "tool_input": {}})
    assert (result.returncode == 0) is allowed
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugin_hooks.py -q`

Expected: scriptsがなくFAIL。

- [ ] **Step 3: 最小policyとhooks.jsonを実装する**

各policyはtool名をprefix/suffixの明示集合で判定する。入力JSONが壊れておりtool名も取れない場合はexit 2。
拒否時はstderrへ操作種別と代替だけを出し、tool inputは出さない。`hooks.json` は `PreToolUse` から
`$CLAUDE_PLUGIN_ROOT/hooks/policy.py` を実行する。

- [ ] **Step 4: token/body非出力テストを追加してGREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_plugin_hooks.py -q`

Expected: PASS、stderrにfixtureのsecret/bodyが含まれない。

---

### Task 7: prompt・運用文書・全体検証

**Files:**
- Modify: `prompts/system.md`
- Modify: `prompts/course.md`
- Modify: `prompts/work.md`
- Modify: `docs/agents.md`
- Modify: `docs/design.md`
- Modify: `deploy/README.md`
- Modify: `README.md`
- Test: repository-wide

**Interfaces:**
- Consumes: Tasks 1–6 and gateway plan
- Produces: implementation-matched operating docs

- [ ] **Step 1: 古い権限説明を列挙する**

Run: `rg -n 'Notion.*読み取り専用|移動・複製・削除.*断|plugin/skills|kei-agent:job|kei-agent:literature' README.md deploy docs prompts src`

Expected: 更新対象だけが表示される。

- [ ] **Step 2: promptと文書を更新する**

大学・研究のNotion全操作、仕事とBoxのread-only、plugin分離、research gatewayの設定・起動・障害時動作を記載する。
promptからskillと重複する検索手順を削り、人格・権限境界・返答形式・原典確認だけを残す。

- [ ] **Step 3: plugin検証を実行する**

Run: `.venv/bin/python -m pytest tests/test_plugins.py tests/test_plugin_hooks.py tests/test_runner.py tests/test_agent_claude.py -q`

Expected: PASS。

- [ ] **Step 4: 全テストとlintを実行する**

Run: `.venv/bin/python -m pytest -q && uvx ruff check . && git diff --check`

Expected: 全件PASS、`All checks passed!`、whitespace errorなし。

- [ ] **Step 5: 差分を監査する**

Run: `git status --short && git diff --stat && git diff --name-only`

Expected: raw tokenなし、担当外plugin共有なし、A2A skill IDとenvelope変更なし、コミット・pushなし。
