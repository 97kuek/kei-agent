# Codex Connectors and Hooks Implementation Plan

> **For the implementation agent:** Required skill: use `superpowers:executing-plans` to execute this plan task-by-task.

**Goal:** Codex を研究エージェントで安全に疎通確認でき、接続済み MCP のみを各エージェントに許可し、実行ライフサイクルで監査可能な最小メタデータを扱えるようにする。

**Architecture:** Codex 固有のプローブと MCP 一覧の解釈を `codex_runtime` に閉じ込める。`run_hooks` は provider 非依存の実行コンテキストと結果を検査・通知し、runner が Codex 呼び出しの前後で利用する。エージェント設定に宣言した connector 名は、agent ごとの許可リストと実際の Codex MCP 一覧の両方を満たす場合だけ許可する。既定プロバイダーは Claude のままにする。

**Tech Stack:** Python 3.12、asyncio、pytest、TOML、Codex CLI。

---

### Task 1: Codex ランタイムのプローブと MCP 解釈を追加する

**Files:**

- Create: `src/kei_agent/codex_runtime.py`
- Create: `tests/test_codex_runtime.py`

**Step 1: Write the failing tests**

`codex exec --json --sandbox readonly` のコマンドに一時作業ディレクトリ、固定プローブ文、persist 無効化が含まれることを検証する。JSONL の `thread.started` と最終 agent message を解析して成功・失敗を返すテスト、`codex mcp list --json` の名前だけを抽出するテスト、認証情報や URL を返さないテストを書く。

**Step 2: Run test to verify it fails**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_codex_runtime.py -q`

Expected: module または対象関数が未実装のため失敗する。

**Step 3: Write minimal implementation**

`ProbeResult` を定義し、プローブは固定文字列 `KEI_AGENT_CODEX_OK` のみを要求する。標準出力 JSONL は thread id と最終 agent message だけを読み、プロンプト全文・ツール入出力・パス・認証情報を保存しない。MCP 検出は JSON 出力のサーバー名だけを `frozenset[str]` として返す。ネットワーク失敗・CLI 不在・不正 JSON は未接続として扱えるエラー値に変換する。

**Step 4: Run test to verify it passes**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_codex_runtime.py -q`

Expected: PASS。

### Task 2: provider 非依存の実行フック境界を追加する

**Files:**

- Create: `src/kei_agent/run_hooks.py`
- Create: `tests/test_run_hooks.py`
- Modify: `src/kei_agent/runner.py`

**Step 1: Write the failing tests**

`RunContext(agent, provider, workspace_kind, model)` と `RunOutcome(context, duration_ms, is_error, session_id)` の生成を検証する。preflight が Codex の connector 宣言を policy 許可リストと実 MCP 一覧に照合し、未接続・不許可 connector では起動前に拒否することを検証する。post-run には許可されたメタデータだけが渡り、プロンプトや応答本文を保持しないことを検証する。

**Step 2: Run test to verify it fails**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_run_hooks.py -q`

Expected: module または対象関数が未実装のため失敗する。

**Step 3: Write minimal implementation**

`run_hooks` に純粋な policy 検証と no-op の post-run 受け口を置く。許可リストは research: `research-notion`, `wandb`、course: `notion`, `box`、work: `microsoft-365` とし、SharePoint は含めない。runner は Codex 実行前に context と MCP 名を渡して preflight し、終了時に計測した所要時間、成功可否、opaque な session id だけで post-run を呼ぶ。既存 Claude `PreToolUse` hooks は変更しない。

**Step 4: Run test to verify it passes**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_run_hooks.py tests/test_runner.py -q`

Expected: PASS。

### Task 3: エージェント設定へ connector 宣言を追加し、skill と運用文書を揃える

**Files:**

- Modify: `src/kei_agent/config.py`
- Modify: `config.toml`
- Modify: `tests/test_themes.py`
- Modify: `plugin/research/skills/managing-wandb/SKILL.md`
- Modify: `.agents/skills/managing-wandb/SKILL.md`
- Modify: `docs/agents.md`

**Step 1: Write the failing tests**

Agent profile の `connectors` が文字列配列として読み込まれ、非配列・重複・未知設定キーが適切に扱われることを検証する。既定設定では全 agent が Claude のままかつ connector 宣言が空であることを検証する。

**Step 2: Run test to verify it fails**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_themes.py -q`

Expected: `connectors` の未対応により失敗する。

**Step 3: Write minimal implementation**

`AgentProfile` に不変の connector 集合を追加する。`config.toml` は provider を Claude のまま保ち、Codex に切り替えるための connector 設定例と「実 MCP 一覧に出たものだけを宣言する」運用を文書化する。W&B skill の YAML description を正しい YAML にし、W&B 未接続時は CLI/SDK/認証情報探索を行わないことを明記する。course/work の Codex 化は、該当 MCP が発見されるまで実施しない。

**Step 4: Run test to verify it passes**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_themes.py tests/test_plugins.py -q`

Expected: PASS。

### Task 4: 回帰検証と実環境の read-only smoke test を行う

**Files:**

- Modify: `docs/agents.md`

**Step 1: Run focused automated tests**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest tests/test_codex_runtime.py tests/test_run_hooks.py tests/test_themes.py tests/test_runner.py tests/test_plugins.py -q`

Expected: PASS。

**Step 2: Run offline regression suite**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run pytest -q --ignore=tests/test_a2a.py --ignore=tests/test_research_agent.py --ignore=tests/test_work_agent.py --ignore=tests/test_notion_gateway.py`

Expected: PASS。

**Step 3: Run static verification**

Run: `UV_CACHE_DIR=/private/tmp/kei-agent-uv-cache uv run python -m compileall -q src`

Run: `git diff --check`

Expected: both commands exit 0。

**Step 4: Run the real Codex smoke test**

Run: the new readonly probe through the logged-in local Codex CLI.

Expected: Codex returns exactly the fixed probe acknowledgement, no repository or Notion data is accessed, and the resulting thread id is recorded only in transient test output.

**Step 5: Document the observed connector state**

Document only connector names discovered by `codex mcp list --json`. Keep course/work on Claude when their required connectors are not visible. Do not install plugins, initiate OAuth, or modify any Notion workspace.
