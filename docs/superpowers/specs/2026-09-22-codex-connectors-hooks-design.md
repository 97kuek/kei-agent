# Codex Connectors and Hooks Design

## Goal

Run the research agent through the user's logged-in Codex CLI, reuse already configured non-SharePoint connectors without copying credentials, and give all runner executions a small, auditable lifecycle boundary.

## Scope and boundaries

The implementation has three independently deployable parts.

1. The research runner gets a read-only Codex smoke-check command and runner lifecycle hooks. The smoke check uses a temporary directory, a fixed no-write prompt, and `--sandbox read-only`; it does not edit `config.toml` or persist a conversation.
2. W&B stays research-only. Its skill remains read-first. A connector profile is discovered from the logged-in Codex configuration and is never recreated from API keys or environment variables. If unavailable, the agent reports `未接続` rather than falling back to the W&B CLI or SDK.
3. Course and work stay on Claude until the corresponding Codex MCP servers are found and policy-checked. The adapter must name only discovered connector IDs, keep course Notion operations within the course profile, keep work read-only, and leave SharePoint unavailable.

## Lifecycle hooks

Hooks live in the runner, not inside a model prompt. A preflight hook receives only the agent name, provider, workspace kind, and selected model. It rejects an impossible provider/profile combination before spawning a process. A post-run hook receives the same identifiers plus duration, success/error status, and an opaque session ID; it never receives prompts, model output, tool arguments, paths outside the workspace, or credentials.

The existing Claude plugin `PreToolUse` hooks remain the tool-level safety layer. Codex does not emulate that hook protocol; it continues to rely on its sandbox and scoped MCP configuration, while the runner lifecycle hooks provide common observability.

## Connector discovery

The discovery command reads only the configured Codex MCP server names and connection status. Server URLs, credentials, OAuth tokens, and tool payloads are never copied into this repository or logs. Names are compared against a fixed allowlist per agent:

- Research: `research-notion` plus an explicitly discovered W&B server.
- Course: configured Notion and Box servers only.
- Work: configured Microsoft 365 server only; SharePoint remains excluded until separately configured.

No auto-enablement, login, installation, or credential migration occurs. A missing connector blocks Codex routing for that agent and keeps its current Claude route active.

## Verification

Unit tests cover hook payload minimisation, profile rejection, and connector-name policy. The real smoke check is opt-in and reports only CLI availability, successful completion, and the opaque Codex thread ID. It must not modify a research workspace. Existing Claude runner and plugin-hook tests remain green.
