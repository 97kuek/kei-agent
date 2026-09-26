"""AI の起動口（Claude も Codex も、どの担当もここだけ）と、経過の読み取り。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from kei_agent import codex_apps, guard
from kei_agent.agent_policy import NOTION_MCP, AgentPolicy
from kei_agent.config import Config, path_without_venv
from kei_agent.execution_contract import ExecutionContract, resolve_contract
from kei_agent.model_policy import ResolvedModel, validate_resolved
from kei_agent.notion import gateway_client_token
from kei_agent.provider_permissions import PROFILE_NAME, CapabilityUnavailable, PermissionProfile, preflight
from kei_agent.themes import Workspace

# 契約の上限に達したときに claude -p が返す文。書き方は版によって違う。
#   `Claude AI usage limit reached|<エポック秒>`
#   `You've hit your session limit · resets 6:30pm (Asia/Tokyo)`
#   `5-hour limit reached ∙ resets 3pm`
# 明ける時刻が古いまま返ることがあるので、過去の時刻はそのまま使わない
_USAGE_LIMIT = re.compile(r"(?:usage|session|hour|weekly)\s+limit\s+reached|hit your (?:usage|session|\w+)\s*limit",
                          re.IGNORECASE)
_LIMIT_EPOCH = re.compile(r"limit reached\|(\d{10,13})")
_LIMIT_RESETS = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
# 明ける時刻が分からないときに、これだけ待ってからやり直す（秒）
UNKNOWN_LIMIT_RESET = -1.0

# claude が終わったあと、プロセスが消えるのを待つ秒数
EXIT_GRACE_SECONDS = 5
# resume しようとした会話が provider 側に残っていないときの文
SESSION_MISSING_MARKERS = ("No conversation found", "no rollout found for")
# Notion ゲートウェイの親の合言葉（子には渡さない）と、子に渡すヘッダーの値の置き場所
GATEWAY_TOKEN_ENV = "KEI_AGENT_NOTION_GATEWAY_TOKEN"
GATEWAY_AUTH_ENV = "KEI_AGENT_NOTION_GATEWAY_AUTH"
# 二の柵のフック1回の上限（plugin/<agent>/hooks/hooks.json と同じ）
HOOK_TIMEOUT_SECONDS = 5
# Claude の担当が持たない Codex の道具（サブエージェント、画像の生成、プラグインの導入の依頼）。どの担当でも切る
CODEX_OFF_FEATURES = ("multi_agent", "image_generation", "tool_suggest")
_CONFIG_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def run_timeout_seconds(config: Config, ws: Workspace, policy: AgentPolicy | None = None) -> float:
    """1回の上限時間（秒）。作業場の指定、担当の指定、config.toml の順に使う。"""
    minutes = ws.timeout_minutes or (policy.timeout_minutes if policy else None) or config.run_timeout_minutes
    return minutes * 60


def notion_mcp_config(config: Config) -> dict:
    """Claude に渡すゲートウェイの MCP。生の Notion トークンではなく、担当の合言葉のヘッダーだけを載せる。"""
    return {"mcpServers": {NOTION_MCP: {
        "type": "http",
        "url": config.notion_gateway_url,
        # build_env が、その担当の合言葉のヘッダーの値を GATEWAY_AUTH_ENV に置く（親の合言葉は渡さない）
        "headers": {"Authorization": f"${{{GATEWAY_AUTH_ENV}}}"},
    }}}


def _checked_key(value: str) -> str:
    if not _CONFIG_KEY.fullmatch(value):
        raise CapabilityUnavailable("接続の設定名を安全に指定できません")
    return value


def codex_hooks_setting(hook: Path) -> str:
    """plugin の二の柵（PreToolUse）を Codex にも掛ける設定。Claude では plugin として同じものが効く。"""
    entry = {"hooks": [{"type": "command", "command": str(hook), "timeout": HOOK_TIMEOUT_SECONDS}]}
    return "hooks.PreToolUse=" + _toml(entry, array=True)


def _toml(value: object, array: bool = False) -> str:
    """設定の上書きに使う TOML のインライン表記（文字列は JSON の書き方がそのまま TOML になる）。"""
    if array:
        return "[" + _toml(value) + "]"
    if isinstance(value, dict):
        # 鍵は引用符で囲む（App の道具の名前 `box.get_file_content` の `.` を、入れ子と読ませない）
        return "{" + ",".join(f"{json.dumps(key)}={_toml(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    return json.dumps(value)


def build_codex_command(config: Config, ws: Workspace, session_id: str | None, recipe: ResolvedModel,
                        contract: ExecutionContract, apps: Mapping[str, str] | None = None) -> list[str]:
    """Codex CLI の非対話 JSONL 実行。制限は Claude と同じ表（contract.policy）から作る。"""
    assert ws.cwd is not None
    policy = contract.policy
    apps = apps or {}
    profile = preflight(config, contract, "codex_cli")
    cmd = [
        config.codex_bin, "exec", "--json", "--strict-config", "--ignore-user-config",
        # 作業場は Git のリポジトリとは限らない（振り分け、大学・仕事の作業場、自己改善の一時ディレクトリ）
        "--skip-git-repo-check", "--cd", str(ws.cwd),
    ]
    for setting in profile.config_overrides:
        cmd += ["--config", setting]
    cmd += ["--model", recipe.model]
    if recipe.reasoning_effort:
        cmd += ["--config", f"model_reasoning_effort={recipe.reasoning_effort}"]
    if contract.prompt_text:
        cmd += ["--config", codex_instructions_setting(contract.prompt_text)]
    # Web の検索は、Claude の WebSearch と同じく表で許した担当だけ（Codex の既定は有効）
    cmd += ["--config", f"web_search={json.dumps('live' if policy.web else 'disabled')}"]
    for feature in CODEX_OFF_FEATURES:
        cmd += ["--disable", feature]
    if policy.files == "none":
        # ファイルを読まない担当には、画像を開く道具も渡さない（Claude の Read と同じく作業場の外を見せない）
        cmd += ["--disable", "view_image"]
    # アカウントの連携は、表に書いた App の、表に書いた読む道具だけ（ほかの道具はモデルに見せない）。
    # apps は表示名 → いまのログインでの ID
    cmd += ["--config", "apps._default.enabled=false"]
    for app in policy.codex_apps:
        if app.name not in apps:
            continue
        key = _checked_key(apps[app.name])
        cmd += ["--config", f"apps.{key}.enabled=true",
                "--config", f"apps.{key}.destructive_enabled=false",
                "--config", f"apps.{key}.default_tools_enabled=false",
                "--config", f"apps.{key}.tools=" + _toml({tool: {"enabled": True} for tool in app.tools}),
                # 渡すのは読む道具だけなので、呼ぶたびの承認は求めない（無人で動く）
                "--config", f'apps.{key}.default_tools_approval_mode="approve"']
    if policy.notion != "none":
        cmd += [
            "--config", f"mcp_servers.{NOTION_MCP}.url={json.dumps(config.notion_gateway_url)}",
            "--config", f'mcp_servers.{NOTION_MCP}.env_http_headers={{Authorization="{GATEWAY_AUTH_ENV}"}}',
            # つながらなければ別の経路に乗り換えず、始める前に止まる
            "--config", f"mcp_servers.{NOTION_MCP}.required=true",
            # 無人で動くので、呼ぶたびの承認は求めない（Claude の dontAsk と同じ）。届く範囲はゲートウェイが決める
            "--config", f'mcp_servers.{NOTION_MCP}.default_tools_approval_mode="approve"',
        ]
        if policy.notion_tools is not None:
            cmd += ["--config", f"mcp_servers.{NOTION_MCP}.enabled_tools={json.dumps(list(policy.notion_tools))}"]
    if contract.skill_dir is not None:
        cmd += ["--config", codex_hooks_setting(contract.skill_dir.parent / "hooks" / "policy.py"),
                # フックは Kei Agent 自身のもの（plugin/<agent>/hooks）なので、信頼の確認を省く
                "--dangerously-bypass-hook-trust"]
    if session_id:
        cmd += ["resume", session_id]
    # prompt は stdin から渡す。`-` を明示しないと、Codex CLI は引数のpromptを待つ。
    cmd += ["-"]
    return cmd


def _build_claude_command(config: Config, ws: Workspace, session_id: str | None,
                          recipe: ResolvedModel, contract: ExecutionContract) -> list[str]:
    policy = contract.policy
    cmd = [
        config.claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        # アカウントの連携（Box・Microsoft 365）は、その担当のプロファイルのユーザー設定から読む。
        # ほかの担当はユーザー設定（フックやプラグイン、広い許可ルール）を持ち込まない
        "--setting-sources", "user" if policy.connectors else "",
        "--settings", json.dumps(guard.build_settings(config, ws, policy), ensure_ascii=False),
        "--permission-mode", "dontAsk",
    ]
    if contract.skill_dir:
        cmd += ["--plugin-dir", str(contract.skill_dir.parent)]
    mcp = notion_mcp_config(config) if policy.notion != "none" else {"mcpServers": {}}
    cmd += ["--mcp-config", json.dumps(mcp, ensure_ascii=False)]
    if not policy.connectors:
        # ユーザーやプロジェクトの MCP も、アカウントの連携も読まない
        cmd.append("--strict-mcp-config")
    if contract.prompt_text:
        # --resume のときは効かない（会話を始めたときの版が残る）。版が変われば本体が会話を始め直す
        cmd += ["--append-system-prompt", contract.prompt_text]
    cmd += ["--model", recipe.model]
    if recipe.reasoning_effort:
        cmd += ["--effort", recipe.reasoning_effort]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def build_command(config: Config, request: ExecutionRequest,
                  contract: ExecutionContract | None = None, apps: Mapping[str, str] | None = None) -> list[str]:
    """解決済み recipe と制限の表だけで Claude/Codex の command を組み立てる。"""
    validate_resolved(request.recipe)
    contract = contract or resolve_contract(config, request)
    if request.recipe.provider == "codex":
        return build_codex_command(config, request.workspace, request.session_id, request.recipe,
                                   contract, apps)
    return _build_claude_command(config, request.workspace, request.session_id, request.recipe, contract)


@dataclass(frozen=True)
class ExecutionRequest:
    """解決済み recipe で一回だけ実行するための入力。

    model と reasoning effort は policy が解決済みの ``recipe`` だけから取り、
    workspace や設定値による上書きを許さない。
    """

    workspace: Workspace
    recipe: ResolvedModel
    session_id: str | None
    channel: str
    thread_ts: str
    read_only: bool = False


async def verify_codex_profile(codex_bin: str, cwd: Path, profile: PermissionProfile) -> None:
    """同じ profile を OS sandbox が起動できることを、モデル実行前に確認する。"""
    command = [codex_bin, "sandbox", "-P", PROFILE_NAME, "-C", str(cwd)]
    for setting in profile.config_overrides:
        command += ["-c", setting]
    command += ["--", "/usr/bin/true"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (OSError, TimeoutError) as exc:
        raise CapabilityUnavailable("Codex の権限を強制できません") from exc
    if proc.returncode != 0:
        raise CapabilityUnavailable("Codex の権限を強制できません")


def codex_instructions_setting(text: str) -> str:
    """正本の指示を Codex の developer instruction にする設定（CLI と App Server で共通）。"""
    return "developer_instructions=" + json.dumps(text, ensure_ascii=False)


def install_agent_skills(contract: ExecutionContract, cwd: os.PathLike[str]) -> None:
    """この実行の agent skill だけを公開する。利用者所有の skill は触らない。"""
    if contract.skill_dir is None:
        clear_managed_skill_directory(Path(cwd))
        return
    install_skill_directory(contract.skill_dir, Path(cwd))


def _managed_skill_sources(manifest: Path) -> dict[str, Path]:
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    except (ValueError, OSError) as e:
        raise RuntimeError("managed skill manifest is invalid") from e
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or Path(name).name != name or not isinstance(source, str)
        for name, source in raw.items()
    ):
        raise RuntimeError("managed skill manifest is invalid")
    return {name: Path(source) for name, source in raw.items()}


def clear_managed_skill_directory(cwd: Path) -> None:
    """manifest に記録された Kei Agent link だけを外す。"""
    target_root = cwd / ".agents" / "skills"
    manifest = target_root / ".kei-agent-managed-skills.json"
    managed = _managed_skill_sources(manifest)
    for name, source in managed.items():
        target = target_root / name
        if not target.is_symlink() or target.resolve() != source.resolve():
            raise RuntimeError(f"managed skill changed by another owner: {name}")
        target.unlink()
    if manifest.exists():
        manifest.write_text("{}", encoding="utf-8")


def install_skill_directory(source_root: Path, cwd: Path) -> None:
    """指定された agent の skill ディレクトリだけを作業場へ反映する。"""
    if not source_root.is_dir():
        raise FileNotFoundError(f"agent skill directory is missing: {source_root}")
    target_root = cwd / ".agents" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    manifest = target_root / ".kei-agent-managed-skills.json"
    managed = _managed_skill_sources(manifest)
    for name, managed_source in managed.items():
        target = target_root / name
        if target.is_symlink() and target.resolve() == managed_source.resolve():
            target.unlink()
        elif target.exists() or target.is_symlink():
            raise RuntimeError(f"managed skill changed by another owner: {name}")
    new_managed: dict[str, str] = {}
    for source in sorted(source_root.iterdir()):
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            continue
        target = target_root / source.name
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"agent skill collides with existing skill: {source.name}")
        target.symlink_to(os.path.relpath(source, target_root), target_is_directory=True)
        new_managed[source.name] = str(source.resolve())
    manifest.write_text(json.dumps(new_managed, ensure_ascii=False), encoding="utf-8")


def build_env(config: Config, base: dict[str, str], channel: str, thread_ts: str,
              policy: AgentPolicy | None = None) -> dict[str, str]:
    """子の環境。Notion の鍵は、その担当のホームにしか届かない合言葉（ヘッダーの値）だけを渡す。"""
    env = guard.strip_env(base)
    master = base.get(GATEWAY_TOKEN_ENV, "").strip()
    env["PATH"] = path_without_venv(base.get("PATH", ""), config.repo_root)
    env["KEI_AGENT_CHANNEL"] = channel
    env["KEI_AGENT_THREAD_TS"] = thread_ts
    if policy is not None and policy.notion != "none" and master:
        env[GATEWAY_AUTH_ENV] = f"Bearer {gateway_client_token(master, policy.name)}"
    return env


def describe_tool(name: str, tool_input: dict) -> str:
    """経過用メッセージに出す、ツール呼び出しの短い説明。"""
    def short(s: str, n: int = 80) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "Bash":
        return "実行している: " + short(tool_input.get("description") or tool_input.get("command", ""))
    if name == "Read":
        return f"読んでいる: {short(tool_input.get('file_path', ''))}"
    if name in ("Write", "NotebookEdit"):
        return f"書いている: {short(tool_input.get('file_path', ''))}"
    if name == "Edit":
        return f"直している: {short(tool_input.get('file_path', ''))}"
    if name == "Glob":
        return f"探している: {short(tool_input.get('pattern', ''))}"
    if name == "Grep":
        return f"調べている: {short(tool_input.get('pattern', ''))}"
    if name == "WebSearch":
        return "Web で検索している: " + short(tool_input.get("query", ""))
    if name == "WebFetch":
        return "Web ページを読んでいる: " + short(tool_input.get("url", ""))
    if name == "Skill":
        return "skill を使っている: " + short(tool_input.get("skill", ""))
    if name == "TodoWrite":
        return "作業の進め方を整理している"
    return name


# 失敗の種類の言い方（RunResult.failure_reason）
FAILURE_LABELS = {"quota": "利用上限", "timeout": "時間切れ", "session_missing": "会話が見つからない",
                  "capability": "選んだ provider では使えない", "runtime": "実行の失敗"}


@dataclass
class RunResult:
    provider: str | None = None
    session_id: str | None = None
    text: str = ""
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    errors: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    timed_out: bool = False
    # Bash の allowed_domains で広げようとした接続先と、そのときの説明。sandbox では断られるので、
    # Kei Agent が依頼者に [許可する] [断る] を聞く（docs/architecture.md）
    requested_domains: list[tuple[str, str]] = field(default_factory=list)
    # 契約の上限に達したときの、明ける時刻（エポック秒）。分からないときは UNKNOWN_LIMIT_RESET
    limit_reset_at: float | None = None
    failure_kind: Literal["quota", "timeout", "session_missing", "capability", "runtime"] | None = None
    _final_candidate: str = field(default="", repr=False)
    # 成功の最後のイベント（result / turn.completed）まで届いたか
    _completed: bool = field(default=False, repr=False)

    @property
    def session_missing(self) -> bool:
        # Claude は "No conversation found"、Codex は "no rollout found for thread id" で断る
        return any(marker in e for e in self.errors for marker in SESSION_MISSING_MARKERS)

    def failure_reason(self, limit: int = 200) -> str:
        """失敗の理由を短く（ログや知らせ用）。エラーの文が無いとき（時間切れ、空の返事）も、何が起きたかを残す。"""
        kind = FAILURE_LABELS.get(self.failure_kind or "", self.failure_kind or "")
        detail = "; ".join(self.errors)
        if not detail and self.failure_kind == "runtime":
            detail = "返事が空か、終了コードだけを残して止まった"
        return "：".join(part for part in (kind, detail) if part)[:limit] or "理由不明"


def apply_event(result: RunResult, event: dict) -> str | None:
    """1行分のイベントを結果に反映する。経過に出す文があれば返す。"""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        result.session_id = event.get("session_id")
        return None
    if etype == "assistant":
        activity = None
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                activity = describe_tool(block.get("name", ""), tool_input)
                result.activities.append(activity)
                known = {d for d, _ in result.requested_domains}
                for domain in tool_input.get("allowed_domains") or []:
                    if isinstance(domain, str) and domain not in known:
                        known.add(domain)
                        result.requested_domains.append((domain, str(tool_input.get("description") or "")))
        return activity
    if etype == "result":
        result.session_id = event.get("session_id") or result.session_id
        result.text = event.get("result") or ""
        result.is_error = bool(event.get("is_error"))
        result.cost_usd = event.get("total_cost_usd")
        result.duration_ms = event.get("duration_ms")
        result.errors = [str(e) for e in event.get("errors") or []]
        result._completed = not result.is_error
        if result.is_error:
            result.limit_reset_at = parse_limit(" ".join([result.text, *result.errors]))
    return None


def apply_codex_event(result: RunResult, event: dict) -> str | None:
    """Codex の JSONL event を、Claude と同じ RunResult に写す。"""
    etype = event.get("type")
    if etype == "thread.started":
        result.session_id = event.get("thread_id")
        return None
    item = event.get("item") or {}
    item_type = item.get("type")
    if etype in {"item.started", "item.completed"} and item_type == "command_execution":
        command = item.get("command") or ""
        activity = describe_tool("Bash", {"command": command})
        result.activities.append(activity)
        return activity
    if (etype == "item.started" and item_type == "mcp_tool_call") or (
            etype == "item.completed" and item_type == "web_search"):
        # Claude と同じ名前で経過に出す（MCP の道具は `mcp__<server>__<tool>`）
        activity = (describe_tool("WebSearch", {"query": item.get("query") or ""}) if item_type == "web_search"
                    else describe_tool(f"mcp__{item.get('server') or ''}__{item.get('tool') or ''}", {}))
        result.activities.append(activity)
        return activity
    if etype == "item.completed" and item_type == "agent_message":
        text = str(item.get("text") or "").strip()
        if text:
            result._final_candidate = text
        return None
    if etype == "turn.completed" and not result.is_error:
        result.text = result._final_candidate
        result._completed = True
        return None
    if etype == "turn.failed":
        error = event.get("error") or {}
        message = str(error.get("message") if isinstance(error, dict) else error)
        result.errors.append(message)
        result.is_error = True
        result.limit_reset_at = parse_limit(message)
    elif etype == "error":
        message = str(event.get("message") or event.get("error") or "Codex のエラー")
        result.errors.append(message)
        result.is_error = True
        result.limit_reset_at = parse_limit(message)
    return None


def finalize_run_result(result: RunResult, returncode: int | None) -> RunResult:
    """プロセス終了まで確認してから、公開候補を確定する。"""
    if result.timed_out:
        result.is_error = True
        result.failure_kind = "timeout"
    elif result.limit_reset_at is not None:
        result.is_error = True
        result.failure_kind = "quota"
    elif result.session_missing:
        result.is_error = True
        result.failure_kind = "session_missing"
    elif returncode != 0 or result.is_error or not result.text.strip():
        result.is_error = True
        result.failure_kind = "runtime"
    if result.is_error:
        result.text = ""
    return result


def parse_limit(text: str, now: float | None = None) -> float | None:
    """契約の上限に当たったか。当たっていれば明ける時刻（エポック秒）、分からなければ UNKNOWN_LIMIT_RESET。"""
    if not _USAGE_LIMIT.search(text):
        return None
    epoch = _LIMIT_EPOCH.search(text)
    if epoch:
        return float(epoch.group(1)[:10])
    when = _LIMIT_RESETS.search(text)
    if not when:
        return UNKNOWN_LIMIT_RESET
    hour, minute, ampm = int(when.group(1)), int(when.group(2) or 0), (when.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return UNKNOWN_LIMIT_RESET
    base = datetime.fromtimestamp(time.time() if now is None else now)
    reset = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # 「3pm に明ける」が今より前なら、明日の 3pm のこと
    return (reset if reset > base else reset + timedelta(days=1)).timestamp()


def kill_group(pid: int) -> None:
    """claude とその中で動いている Bash を、プロセスグループごと止める。

    start_new_session=True で起動しているので、グループIDは claude の pid と同じ。
    claude 本体を回収したあとでも、残った子プロセスを止められるよう getpgid は使わない。
    プロセスグループで止める処理はここを正本にする（kei_agent_a2a からも使う）。
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def run_model(
    config: Config,
    request: ExecutionRequest,
    prompt: str,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
) -> RunResult:
    """用途別 recipe を解決済みの実行要求を一度だけ走らせる。

    この関数だけが CLI を起動する。呼び出し元は provider / model / effort を個別に
    指定できず、``ExecutionRequest.recipe`` を model policy で解決して渡す。
    """
    ws = request.workspace
    assert ws.cwd is not None
    recipe = request.recipe
    validate_resolved(recipe)
    is_codex = recipe.provider == "codex"
    contract = resolve_contract(config, request)
    policy = contract.policy
    apps: dict[str, str] = {}
    if is_codex:
        try:
            # アカウントの連携は、いまのログインでの ID を表示名から引く（ID は保存しない）
            apps = await codex_apps.app_ids(config.codex_bin, [app.name for app in policy.codex_apps])
            await verify_codex_profile(config.codex_bin, ws.cwd, preflight(config, contract, "codex_cli"))
        except (CapabilityUnavailable, codex_apps.AppsUnavailable) as exc:
            return RunResult(provider=recipe.provider, is_error=True, errors=[str(exc)], failure_kind="capability")
        install_agent_skills(contract, ws.cwd)
    proc = await asyncio.create_subprocess_exec(
        *build_command(config, request, contract, apps),
        cwd=ws.cwd,
        env=build_env(config, dict(os.environ), request.channel, request.thread_ts, policy),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        # 上限時間で止めるとき、中で動いている Bash などもまとめて止められるよう、別のプロセスグループにする
        start_new_session=True,
    )
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()

    result = RunResult(provider=recipe.provider)

    async def read_stdout() -> None:
        async for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # 途中の assistant / agent_message は内部の思考や下書きを含み得る。
            # Slack / A2A へは流さず、完了後の final だけを採用する。
            activity = apply_codex_event(result, event) if is_codex else apply_event(result, event)
            if activity and on_activity:
                await on_activity(activity)
            if event.get("type") == "result" or event.get("type") in {"turn.completed", "turn.failed", "error"}:
                # claude の最後のイベント。ここで読むのをやめる。
                # Bash が残したプロセスが出力を握っていると、EOF はいつまでも来ない
                return

    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        await asyncio.wait_for(read_stdout(), timeout=run_timeout_seconds(config, ws, policy))
    except TimeoutError:
        result.timed_out = True
        result.is_error = True
    except ValueError:
        # stream-json の1行が limit を超えた
        result.is_error = True
        result.errors.append("claude の出力が大きすぎて読めませんでした")
    finally:
        if not result.timed_out:
            # claude がセッションを保存して終わるのを、少しだけ待つ
            with suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)
        # 正常に終わっていれば空振りする。Bash が残したプロセスがいれば、ここでまとめて止める
        kill_group(proc.pid)
        stderr = await _drain(stderr_task)
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)
    returncode = proc.returncode
    if result._completed and returncode == -signal.SIGKILL:
        # 成功の result まで届いたあと、終わらないので猶予のあとで止めた。答えはそのまま使う
        returncode = 0
    if returncode and not result.errors and stderr:
        result.is_error = True
        result.errors.append(stderr[-2000:])
    finalize_run_result(result, returncode)
    return result


async def _drain(task: asyncio.Task) -> str:
    """stderr を待つ。子プロセスが握ったままでも、待ち続けない。"""
    try:
        return (await asyncio.wait_for(task, timeout=EXIT_GRACE_SECONDS)).decode("utf-8", "replace").strip()
    except (TimeoutError, asyncio.CancelledError, ValueError):
        return ""
