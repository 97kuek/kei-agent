"""claude -p の起動と、stream-json の読み取り。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from kei_agent import guard, run_hooks
from kei_agent.config import Config, path_without_venv
from kei_agent.execution_contract import ExecutionContract, resolve_contract
from kei_agent.model_policy import ResolvedModel, validate_resolved
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
# このファイルが動かすのは研究の claude だけ。skill も MCP も研究のものに限る
AGENT = "research"
# 研究ホームだけを操作できる Notion（src/kei_agent_notion_gateway）。合言葉は claude が環境変数から入れる
NOTION_MCP = "research-notion"
GATEWAY_TOKEN_ENV = "KEI_AGENT_NOTION_GATEWAY_TOKEN"
GATEWAY_AUTH_ENV = "KEI_AGENT_NOTION_GATEWAY_AUTH"


def system_prompt_text(config: Config) -> str:
    path = config.system_prompt_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def system_prompt_version(config: Config) -> str:
    """研究 session の指示・skill の版。変更時は古い会話を再開しない。"""
    from kei_agent.execution_contract import prompt_fingerprint

    return prompt_fingerprint(system_prompt_text(config), config.agent_plugin_dir(AGENT) / "skills")


def run_timeout_seconds(config: Config, ws: Workspace) -> float:
    """claude 1回の上限時間（秒）。ワークスペースの指定があれば、そちらを使う。"""
    return (ws.timeout_minutes or config.run_timeout_minutes) * 60


def notion_mcp_config(config: Config) -> dict:
    """研究 Claude に渡す MCP の設定。生の Notion トークンではなく、入口の合言葉だけを載せる。"""
    return {"mcpServers": {NOTION_MCP: {
        "type": "http",
        "url": config.notion_gateway_url,
        # build_env は合言葉そのもの（GATEWAY_TOKEN_ENV）を子に渡さず、ヘッダーの値だけを GATEWAY_AUTH_ENV に置く
        "headers": {"Authorization": f"${{{GATEWAY_AUTH_ENV}}}"},
    }}}


def build_codex_command(config: Config, ws: Workspace, session_id: str | None,
                        recipe: ResolvedModel, *, actor: str = AGENT,
                        read_only: bool = False, contract: ExecutionContract | None = None) -> list[str]:
    """Codex CLI の非対話 JSONL 実行。認証は codex CLI のログイン状態に任せる。"""
    assert ws.cwd is not None
    # ルーターは状態DBの下（Gitリポジトリ外）で、接続先も作業用MCPも不要な分類だけをする。
    is_router = actor == "router" or ws.system_prompt == config.repo_root / "prompts" / "router.md"
    if contract is None:
        contract = resolve_contract(config, ExecutionRequest(ws, recipe, session_id, "", "", read_only))
    profile = preflight(config, contract, "codex_cli")
    cmd = [
        config.codex_bin, "exec", "--json", "--strict-config", "--ignore-user-config",
        "--cd", str(ws.cwd),
    ]
    for setting in profile.config_overrides:
        cmd += ["--config", setting]
    if is_router:
        cmd.append("--skip-git-repo-check")
    cmd += ["--model", recipe.model]
    if recipe.reasoning_effort:
        cmd += ["--config", f"model_reasoning_effort={recipe.reasoning_effort}"]
    if contract and contract.prompt_text:
        cmd += codex_instruction_config(contract)
    # 個人の App connector（Google Calendar等）は研究にもルーターにも渡さない。
    cmd += ["--config", "apps._default.enabled=false"]
    if actor == "research" and not read_only and not is_router:
        # 研究ホームの外へ届く Notion を持ち込まず、検査済みgatewayだけを渡す。
        cmd += [
            "--config", f"mcp_servers.{NOTION_MCP}.url={json.dumps(config.notion_gateway_url)}",
            "--config", f'mcp_servers.{NOTION_MCP}.env_http_headers={{Authorization="{GATEWAY_AUTH_ENV}"}}',
            "--config", f"mcp_servers.{NOTION_MCP}.enabled=true",
        ]
    if session_id:
        cmd += ["resume", session_id]
    # prompt は stdin から渡す。`-` を明示しないと、Codex CLI は引数のpromptを待つ。
    cmd += ["-"]
    return cmd


def _build_claude_command(config: Config, ws: Workspace, session_id: str | None,
                          recipe: ResolvedModel, contract: ExecutionContract, *, actor: str = AGENT,
                          read_only: bool = False) -> list[str]:
    cmd = [
        config.claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        # ユーザー設定（フックやプラグイン、広い許可ルール）を持ち込まない
        "--setting-sources", "",
        "--settings", json.dumps(guard.build_settings(config, ws, read_only=read_only), ensure_ascii=False),
        "--permission-mode", "dontAsk",
    ]
    if contract.skill_dir:
        cmd += ["--plugin-dir", str(contract.skill_dir.parent)]
    if actor == "research" and not read_only:
        # 研究ホームの外へ届く Notion を持ち込ませない。ユーザーやプロジェクトの MCP も読まない
        cmd += ["--mcp-config", json.dumps(notion_mcp_config(config), ensure_ascii=False), "--strict-mcp-config"]
    if contract.prompt_text:
        # --resume のときは効かない（会話を始めたときの版が残る）。版が変われば assistant が会話を始め直す
        cmd += ["--append-system-prompt", contract.prompt_text]
    cmd += ["--model", recipe.model]
    if recipe.reasoning_effort:
        cmd += ["--effort", recipe.reasoning_effort]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def build_command(config: Config, request: ExecutionRequest,
                  contract: ExecutionContract | None = None) -> list[str]:
    """解決済み recipe だけで Claude/Codex の command を組み立てる。"""
    validate_resolved(request.recipe)
    ws = request.workspace
    contract = contract or resolve_contract(config, request)
    if request.recipe.provider == "codex":
        return build_codex_command(config, ws, request.session_id, request.recipe,
                                   actor=request.recipe.actor, read_only=contract.read_only, contract=contract)
    return _build_claude_command(config, ws, request.session_id, request.recipe, contract,
                                 actor=request.recipe.actor, read_only=contract.read_only)


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


def codex_instruction_config(contract: ExecutionContract) -> list[str]:
    return ["--config", codex_instructions_setting(contract.prompt_text)]


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
              *, include_gateway_auth: bool = True) -> dict[str, str]:
    env = guard.strip_env(base)
    token = env.pop(GATEWAY_TOKEN_ENV, "")
    env["PATH"] = path_without_venv(base.get("PATH", ""), config.repo_root)
    env["KEI_AGENT_CHANNEL"] = channel
    env["KEI_AGENT_THREAD_TS"] = thread_ts
    if include_gateway_auth and token:
        env[GATEWAY_AUTH_ENV] = f"Bearer {token}"
    else:
        env.pop(GATEWAY_AUTH_ENV, None)
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


async def stop_group(proc) -> None:
    """プロセスグループごと止めて、回収まで少しだけ待つ。"""
    kill_group(proc.pid)
    with suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)


async def run_model(
    config: Config,
    request: ExecutionRequest,
    prompt: str,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    on_text: Callable[[str], Awaitable[None]] | None = None,
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
    context = run_hooks.RunContext(
        agent=recipe.actor,
        provider=recipe.provider,
        workspace_kind=ws.kind.value,
        model=recipe.model,
    )
    contract = resolve_contract(config, request)
    if is_codex:
        # --ignore-user-config で起動するので、ユーザー設定の MCP は実行時に存在しない。
        # この回に明示注入する gateway だけを「利用可能」と扱う。
        configured_connectors = frozenset({NOTION_MCP})
        connectors = config.agent_profiles.get(recipe.actor)
        run_hooks.preflight(context, frozenset(getattr(connectors, "connectors", ())), configured_connectors)
        try:
            await verify_codex_profile(config.codex_bin, ws.cwd, preflight(config, contract, "codex_cli"))
        except CapabilityUnavailable as exc:
            return RunResult(provider=recipe.provider, is_error=True, errors=[str(exc)], failure_kind="capability")
        install_agent_skills(contract, ws.cwd)
    started_at = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *build_command(config, request, contract),
        cwd=ws.cwd,
        env=build_env(config, dict(os.environ), request.channel, request.thread_ts,
                      include_gateway_auth=recipe.actor == AGENT and not contract.read_only),
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
            # Slack / A2A へ on_text では流さず、完了後の final だけを採用する。
            activity = apply_codex_event(result, event) if is_codex else apply_event(result, event)
            if activity and on_activity:
                await on_activity(activity)
            if event.get("type") == "result" or event.get("type") in {"turn.completed", "turn.failed", "error"}:
                # claude の最後のイベント。ここで読むのをやめる。
                # Bash が残したプロセスが出力を握っていると、EOF はいつまでも来ない
                return

    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        await asyncio.wait_for(read_stdout(), timeout=run_timeout_seconds(config, ws))
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
    run_hooks.post_run(run_hooks.RunOutcome(
        context=context,
        duration_ms=round((time.monotonic() - started_at) * 1000),
        is_error=result.is_error,
        session_id=result.session_id,
    ))
    return result


async def _drain(task: asyncio.Task) -> str:
    """stderr を待つ。子プロセスが握ったままでも、待ち続けない。"""
    try:
        return (await asyncio.wait_for(task, timeout=EXIT_GRACE_SECONDS)).decode("utf-8", "replace").strip()
    except (TimeoutError, asyncio.CancelledError, ValueError):
        return ""
