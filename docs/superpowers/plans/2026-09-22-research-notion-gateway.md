# Research Notion Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 研究Claudeへ生のNotionトークンを渡さず、研究ホーム配下だけを全操作できるlocalhost MCP gatewayを提供する。

**Architecture:** `kei-agent-notion-gateway` は `mcp` Python SDKのStreamable HTTPサーバーとして127.0.0.1:8791に常駐する。全toolは `ResearchNotion` を通り、対象と親を研究ホームの子孫と確認してから既存 `Notion` clientを呼ぶ。

**Tech Stack:** Python 3.13、MCP Python SDK v2、Starlette、uvicorn、pytest

**Spec:** `docs/superpowers/specs/2026-09-22-agent-skills-hooks-design.md`

## Global Constraints

- `NOTION_TOKEN` はgatewayプロセスだけが読む。
- gatewayは127.0.0.1以外へbindしない。
- 空の `KEI_AGENT_NOTION_GATEWAY_TOKEN` では起動しない。
- 研究ホーム外、親不明、循環、探索上限超過は拒否する。
- 監査ログにtoken、本文、property値、検索結果を出さない。
- コミットとpushは依頼されるまで行わない。

---

### Task 1: Gateway設定と起動境界

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/kei_agent/config.py`
- Create: `src/kei_agent_notion_gateway/__init__.py`
- Create: `src/kei_agent_notion_gateway/config.py`
- Test: `tests/test_notion_gateway.py`

**Interfaces:**
- Produces: `GatewayConfig(host: str, port: int, token: str, notion_token: str, state_path: Path)`
- Produces: `load_gateway_config(config: Config, env: Mapping[str, str]) -> GatewayConfig`

- [ ] **Step 1: 設定の失敗テストを書く**

```python
def test_gateway_requires_both_tokens(config):
    from kei_agent_notion_gateway.config import load_gateway_config

    with pytest.raises(RuntimeError, match="KEI_AGENT_NOTION_GATEWAY_TOKEN"):
        load_gateway_config(config, {"NOTION_TOKEN": "notion"})
    with pytest.raises(RuntimeError, match="NOTION_TOKEN"):
        load_gateway_config(config, {"KEI_AGENT_NOTION_GATEWAY_TOKEN": "gateway"})
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py::test_gateway_requires_both_tokens -q`

Expected: import errorでFAIL。

- [ ] **Step 3: 設定を実装する**

```python
@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    token: str
    notion_token: str
    state_path: Path

def load_gateway_config(config: Config, env: Mapping[str, str]) -> GatewayConfig:
    token = env.get("KEI_AGENT_NOTION_GATEWAY_TOKEN", "").strip()
    notion_token = env.get("NOTION_TOKEN", "").strip()
    if not token:
        raise RuntimeError("KEI_AGENT_NOTION_GATEWAY_TOKEN がありません")
    if not notion_token:
        raise RuntimeError("NOTION_TOKEN がありません")
    return GatewayConfig("127.0.0.1", 8791, token, notion_token, config.state_dir / "notion.json")
```

`pyproject.toml` に `mcp>=2,<3` と `kei-agent-notion-gateway = "kei_agent_notion_gateway.app:main"` を追加し、
`module-name` に `kei_agent_notion_gateway` を加える。

- [ ] **Step 4: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py::test_gateway_requires_both_tokens -q`

Expected: PASS。

---

### Task 2: 研究ホームの子孫判定

**Files:**
- Create: `src/kei_agent_notion_gateway/scope.py`
- Test: `tests/test_notion_gateway.py`

**Interfaces:**
- Produces: `ScopeError(RuntimeError)`
- Produces: `ResearchScope(notion: Notion, root_id: str, max_depth: int = 100)`
- Produces: `ResearchScope.require(item_id: str) -> None`

- [ ] **Step 1: 子孫・範囲外・循環テストを書く**

```python
def test_scope_accepts_root_and_descendants():
    scope = ResearchScope(FakeNotion(parents={"child": "root", "grand": "child"}), "root")
    scope.require("root")
    scope.require("grand")

@pytest.mark.parametrize("parents,item", [({}, "outside"), ({"a": "b", "b": "a"}, "a")])
def test_scope_rejects_unknown_and_cycles(parents, item):
    with pytest.raises(ScopeError):
        ResearchScope(FakeNotion(parents=parents), "root").require(item)
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py -k scope -q`

Expected: `ResearchScope` がなくFAIL。

- [ ] **Step 3: 親取得とfail-closed判定を実装する**

```python
class ResearchScope:
    def require(self, item_id: str) -> None:
        seen: set[str] = set()
        current = item_id
        for _ in range(self.max_depth):
            if current == self.root_id:
                return
            if current in seen:
                raise ScopeError("Notion の親関係が循環しています")
            seen.add(current)
            current = self._parent_id(current)
            if not current:
                raise ScopeError("研究ホーム外の対象です")
        raise ScopeError("Notion の親階層が深すぎます")
```

`_parent_id` は `/pages/{id}`、失敗時は `/blocks/{id}`、database/data sourceの親情報の順で取得し、
Notion APIが返した `parent` の `page_id` / `database_id` / `block_id` を正規化する。取得不能を許可扱いにしない。

- [ ] **Step 4: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py -k scope -q`

Expected: PASS。

---

### Task 3: 型付きNotion操作

**Files:**
- Create: `src/kei_agent_notion_gateway/service.py`
- Test: `tests/test_notion_gateway.py`

**Interfaces:**
- Produces: `ResearchNotion(notion: Notion, scope: ResearchScope, audit: Logger)`
- Produces: `read`, `search`, `query`, `create_page`, `update_page`, `append_blocks`, `archive`, `move`, `duplicate`, `create_database`, `update_database`

- [ ] **Step 1: すべての変更対象を検証するテストを書く**

```python
def test_move_checks_source_and_destination(service, scope):
    service.move("source", "destination")
    assert scope.required == ["source", "destination"]

def test_create_checks_parent(service, scope):
    service.create_page("parent", {"title": "x"}, [])
    assert scope.required == ["parent"]
```

`duplicate` はsourceとdestination、`create_database` はparent、update/archive/readはtargetを検証するテストを
parameterizeして全操作を覆う。

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py -k 'move or create or operation' -q`

Expected: `ResearchNotion` がなくFAIL。

- [ ] **Step 3: 各操作をNotion APIへ写像する**

```python
def create_page(self, parent_id: str, properties: dict, children: list[dict]) -> dict:
    self.scope.require(parent_id)
    return self._call("create_page", "POST", "/pages", {
        "parent": {"type": "page_id", "page_id": parent_id},
        "properties": properties,
        "children": children,
    }, parent_id)

def archive(self, target_id: str) -> dict:
    self.scope.require(target_id)
    return self._call("archive", "PATCH", f"/pages/{target_id}", {"archived": True}, target_id)
```

`_call` は成功時と失敗時に `extra={"operation", "target_id", "ok", "error_type"}` だけをlogへ渡す。
任意method/pathを外から受ける関数は公開しない。

- [ ] **Step 4: ログ秘匿テストを書く**

```python
def test_audit_omits_body_and_tokens(service, caplog):
    service.create_page("parent", {"secret": "do-not-log"}, [])
    assert "do-not-log" not in caplog.text
    assert "Bearer" not in caplog.text
```

- [ ] **Step 5: GREENを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py -k 'operation or audit or move or create' -q`

Expected: PASS。

---

### Task 4: MCPサーバーとBearer認証

**Files:**
- Create: `src/kei_agent_notion_gateway/app.py`
- Test: `tests/test_notion_gateway.py`

**Interfaces:**
- Produces: `build_mcp(service: ResearchNotion) -> MCPServer`
- Produces: `build_app(settings: GatewayConfig, service: ResearchNotion) -> Starlette`

- [ ] **Step 1: tool一覧と認証テストを書く**

```python
async def test_mcp_requires_gateway_token(app):
    async with TestClient(app) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/mcp", json={})).status_code == 401

def test_mcp_exposes_only_typed_research_tools(mcp):
    assert set(tool_names(mcp)) == {
        "read", "search", "query", "create_page", "update_page", "append_blocks",
        "archive", "move", "duplicate", "create_database", "update_database",
    }
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py -k mcp -q`

Expected: appがなくFAIL。

- [ ] **Step 3: MCP toolとASGI middlewareを実装する**

`MCPServer("research-notion")` に上記11個の `@mcp.tool()` を登録する。`streamable_http_app(json_response=True)` を
Starletteへmountし、`/health` だけを認証middlewareの例外にする。middlewareは
`Authorization == f"Bearer {settings.token}"` を定数時間比較し、不一致を401にする。

- [ ] **Step 4: 実MCPのin-process往復を通す**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py -q`

Expected: 全件PASS。

---

### Task 5: launchdと運用文書

**Files:**
- Create: `deploy/run-notion-gateway.sh`
- Create: `deploy/com.kei-agent.notion-gateway.plist.template`
- Modify: `deploy/install.sh`
- Modify: `deploy/README.md`
- Modify: `docs/agents.md`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Consumes: `kei-agent-notion-gateway`
- Produces: `deploy/install.sh notion-gateway`

- [ ] **Step 1: deploy構成テストを書く**

```python
def test_notion_gateway_has_launchd_files():
    assert Path("deploy/run-notion-gateway.sh").exists()
    assert Path("deploy/com.kei-agent.notion-gateway.plist.template").exists()
    assert "notion-gateway" in Path("deploy/install.sh").read_text()
```

- [ ] **Step 2: REDを確認する**

Run: `.venv/bin/python -m pytest tests/test_deploy.py -k notion_gateway -q`

Expected: ファイルがなくFAIL。

- [ ] **Step 3: 起動ファイルと設定手順を追加する**

`run-notion-gateway.sh` は共通secretを読み、`uv run --frozen kei-agent-notion-gateway` をexecする。
`deploy/README.md` に次を記載する。

```zsh
# Notion API tokenではなく、研究Claudeからlocalhost gatewayへ接続する合言葉
export KEI_AGENT_NOTION_GATEWAY_TOKEN="$(openssl rand -hex 32)"
deploy/install.sh notion-gateway
```

生成値を一度保存してgatewayと研究エージェントで同じ値を読むこと、`NOTION_TOKEN` はgateway側だけが読むことを書く。

- [ ] **Step 4: 全gatewayテストとlintを通す**

Run: `.venv/bin/python -m pytest tests/test_notion_gateway.py tests/test_deploy.py -q && uvx ruff check src/kei_agent_notion_gateway tests/test_notion_gateway.py`

Expected: PASS、`All checks passed!`。
