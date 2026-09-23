# Agent Model Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** エージェント別文書と、研究の作業深度を解決する更新可能なモデルレシピを追加する。

**Architecture:** `ModelRecipe` を `Config` に読み込み、agentの既定レシピと研究依頼の分類から `Workspace` の model/effort を決める。モデル名は config のレシピにのみ置き、文書は共通索引と担当別ページに分割する。

**Tech Stack:** Python 3.13、tomllib、pytest、Slack Bolt、Markdown

**Spec:** `docs/superpowers/specs/2026-09-23-agent-model-policy-design.md`

## Global Constraints

- provider の暗黙切替・モデルの自動フォールバックをしない。
- 定型の大学・仕事handlerはモデルを起動しない。
- モデル名更新は `config.toml` のレシピだけで行える。
- 既存の App Home のprovider/model/effort上書きとの互換性を保つ。

---

### Task 1: モデルレシピを設定として読む

**Files:**
- Modify: `src/kei_agent/config.py`
- Modify: `config.toml`
- Test: `tests/test_themes.py`

- [ ] レシピ名、provider、model、reasoning_effortを検証する失敗テストを書く。
- [ ] `ModelRecipe`、`Config.model_recipes`、`AgentProfile.default_recipe` を追加する。
- [ ] `model_recipe_for(agent, name)` を実装し、provider不一致を拒否する。
- [ ] 設定解析テストを実行する。

### Task 2: 研究依頼を深度へ分類する

**Files:**
- Modify: `src/kei_agent/research.py`
- Modify: `src/kei_agent/themes.py`
- Modify: `src/kei_agent_research/executor.py`
- Modify: `src/kei_agent/runner.py`
- Test: `tests/test_research_agent.py`

- [ ] `[[deep]]` と深い設計語がroutine語より優先する失敗テストを書く。
- [ ] `research.recipe_for_prompt()` と本文の上書き除去を実装する。
- [ ] A2A payload と Workspace にrecipeを通し、Codex command が対応するmodel/effortを使うようにする。
- [ ] 研究エージェントのテストを実行する。

### Task 3: agent 文書を分離する

**Files:**
- Modify: `docs/agents.md`
- Create: `docs/agents/orchestrator.md`
- Create: `docs/agents/research.md`
- Create: `docs/agents/course.md`
- Create: `docs/agents/work.md`
- Create: `docs/model-policy.md`

- [ ] 共通文書を索引・共通契約・参照先に縮める。
- [ ] 4つの担当文書とモデル更新手順を作る。
- [ ] 各文書の権限、モデル判断、運用手順が矛盾しないことを確認する。

### Task 4: 統合検証

**Files:**
- Test: `tests/test_themes.py`
- Test: `tests/test_research_agent.py`
- Test: `tests/test_runner.py`

- [ ] 対象テストを実行する。
- [ ] `uvx ruff check src tests` と `git diff --check` を実行する。
