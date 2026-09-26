"""Kei Agent の柵。sandbox の設定、読ませない場所、操作してよい人、取り込んでよい差分の判定。

このファイルと `config.toml`、`deploy/` は、Kei Agent 自身に直させない（docs/architecture.md）。
ここに触れた差分は、中身を見る前に捨てる。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kei_agent.agent_policy import AgentPolicy
    from kei_agent.config import Config
    from kei_agent.themes import Workspace

# sandbox の中の Bash から読ませない場所。sandbox は既定で PC 全体を読めるので、
# 環境変数からトークンを外しても、置き場所のファイルはそのまま読めてしまう。
# 使うたびに更新するトークン（`<state_dir>/secrets`）は state_dir で変わるので、config.py で足す
DEFAULT_DENY_READ = (
    "~/.config/kei-agent/secrets",   # Kei Agent の秘密情報の既定の置き場所（config.toml の [paths] secrets）
    "~/.config/zsh/local",   # 作者の環境の秘密情報の置き場所（[paths] secrets で指している）
    "~/.ssh",
    "~/.aws",
    "~/.claude",             # Claude Code の認証情報
    "~/.claude-personal",    # 大学の連携を付けた個人アカウントのプロファイル（deploy/README.md）
    "~/.claude-work",        # 仕事の連携を付けた会社アカウントのプロファイル
    "~/.codex",              # Codex の認証情報とローカル設定
    "~/.config/gh",          # バックアップ先への push 権限
    "~/.netrc",
    "~/.git-credentials",
    "~/.config/git/credentials",
)

# claude -p の子プロセスに渡さない環境変数。Bash から Slack や Notion のトークンが見えないようにする。
# Notion の鍵（NOTION_TOKEN）もゲートウェイの親の合言葉（KEI_AGENT_NOTION_GATEWAY_TOKEN）も、どの子にも渡さない。
# 研究の claude には runner.build_env が研究用の合言葉だけを足す
STRIPPED_ENV_PREFIXES = ("SLACK_", "NOTION_", "KEI_AGENT_NOTION_", "KEI_AGENT_ALLOWED_", "KEI_AGENT_A2A_",
                         "CLAUDECODE", "CLAUDE_CODE_", "VIRTUAL_ENV",
                         # ドメインごとの鍵（Box・Moodle・Microsoft・Toggl）。エージェントの claude にも渡さない
                         "BOX_", "MOODLE_", "MS_", "TOGGL_", "OPENAI_API_KEY", "CODEX_API_KEY")
# 上の prefix に当たっても、子プロセスに残すもの
KEPT_CLAUDE_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)
# prefix で書けない、Kei Agent 自身の合言葉（KEI_AGENT_*_TOKEN など）。あとから増えても渡さない
_KEI_AGENT_SECRET_ENV = re.compile(r"^KEI_AGENT_\w*(?:TOKEN|SECRET|PASSWORD|API_KEY)$")

# 制限の表で持たないときに、名指しで断る Claude の道具（ユーザー設定に広い許可があっても、断るほうが強い）
SEARCH_TOOLS = ("Glob", "Grep")
# Edit の許可は Write と NotebookEdit にも効くので、書けない担当ではまとめて断る
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
SUBAGENT_TOOLS = ("Task", "Agent")
# アカウントに付いた Notion 連携（claude.ai）。Notion はゲートウェイだけを使う
ACCOUNT_NOTION = "mcp__claude_ai_Notion"

# Kei Agent 自身に直させないもの（リポジトリからの相対パス）
# config.example.toml は、新しく使う人の既定の柵（読ませない場所・接続先）になる。本物の設定はリポジトリの外
PROTECTED_PATHS = ("src/kei_agent/guard.py", "config.example.toml", "deploy/")
# 依存するライブラリが変わる差分。取り込む前の確認で、いちばん上に出す
DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock")
# 差分に入っていてはいけない文字列（秘密情報）
SECRET_PATTERNS = (
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"toggl_sk_[0-9a-f]{16,}"),
    re.compile(r"ntn_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
# 1つのファイルの変更量の上限。研究データや重みが紛れ込むのを防ぐ
MAX_DIFF_BYTES = 1_000_000

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,61}}[a-z0-9]$")


def is_owner(config: Config, user_id: str | None) -> bool:
    """依頼者本人か。依頼を受けるのも、ボタンを押せるのも、この人だけ。"""
    return bool(config.allowed_user_id) and user_id == config.allowed_user_id


def valid_domain(domain: str, allow_wildcard: bool = False) -> bool:
    """ぴったりのドメイン名か。allow_wildcard なら先頭の `*.` だけ許す（App Home から自分で足すとき）。"""
    domain = domain.strip().lower()
    if allow_wildcard and domain.startswith("*."):
        domain = domain[2:]
    return bool(_DOMAIN.match(domain))


def _abs_rule(tool: str, path: Path) -> str:
    # 権限ルールで絶対パスを書くときは // で始める
    return f"{tool}(/{path}/**)"


def read_roots(config: Config, ws: Workspace) -> tuple[Path, ...]:
    """そのワークスペースで読んでよい範囲の根。書き込み先（Edit）は、これとは別に ws.cwd だけ。"""
    from kei_agent.themes import ChannelKind
    assert ws.cwd is not None
    if ws.kind is ChannelKind.OVERVIEW:
        # 各テーマを読む。作業場は ~/research の外にあるので、そこも読めるようにする
        return (config.research_root, ws.cwd)
    if ws.kind is ChannelKind.IMPROVE:
        return (config.repo_root,)    # 案を考えるために Kei Agent のコードを読む。書き込みは作業用の一時ディレクトリだけ
    return (ws.cwd,)                  # テーマと、自分を直すときの worktree


def claude_permissions(config: Config, ws: Workspace, policy: AgentPolicy) -> dict[str, list[str]]:
    """制限の表から、Claude に許す道具と断る道具を作る（dontAsk なので、許していないものは使えない）。

    アカウントの連携を使う担当はユーザー設定を読むので、そこに広い許可があっても効かないよう、
    表にない道具は名指しで断る。
    """
    from kei_agent.agent_policy import NOTION_MCP

    assert ws.cwd is not None
    allow: list[str] = []
    if policy.files == "none":
        # 読めるのは自分の作業場だけ（前提のメモ）。探す道具は渡さない
        allow.append(_abs_rule("Read", ws.cwd))
    else:
        allow += [_abs_rule("Read", root) for root in read_roots(config, ws)] + ["Glob", "Grep"]
    if policy.files == "write":
        allow.append(_abs_rule("Edit", ws.cwd))
    if policy.shell:
        allow.append("Bash")
    if policy.web:
        allow += ["WebSearch", "WebFetch"]
    if policy.plugin:
        allow.append("Skill")
    allow.append("TodoWrite")
    if policy.notion_tools is None:
        allow.append(f"mcp__{NOTION_MCP}")
    else:
        allow += [f"mcp__{NOTION_MCP}__{tool}" for tool in policy.notion_tools]
    for connector in policy.connectors:
        allow += connector.claude_names()
    deny = [# 秘密情報の置き場所。sandbox は Bash にしか効かないので、読む道具（Read・Grep・Glob）でも塞ぐ。
            # フォルダ（~/.ssh）とファイル（~/.netrc）の両方の書き方で書く
            *(rule for path in config.deny_read for rule in (f"Read(/{path})", _abs_rule("Read", path))),
            *(SEARCH_TOOLS if policy.files == "none" else ()),
            *(EDIT_TOOLS if policy.files != "write" else ()),
            *(() if policy.shell else ("Bash",)),
            *(() if policy.web else ("WebSearch", "WebFetch")),
            *(() if policy.plugin else ("Skill",)),
            *SUBAGENT_TOOLS,
            # Notion はゲートウェイだけ。アカウントに付いた Notion 連携は、どの担当にも使わせない
            ACCOUNT_NOTION]
    return {"allow": allow, "deny": deny}


def build_settings(config: Config, ws: Workspace, policy: AgentPolicy) -> dict:
    """実行境界で確定した権限だけを Claude に渡す。"""
    assert ws.cwd is not None
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {
                "allowedDomains": list(dict.fromkeys(config.allowed_domains + ws.allowed_domains)),
                "strictAllowlist": True,
            },
            "filesystem": {
                "allowWrite": [str(p) for p in config.allow_write],
                # sandbox は既定で PC 全体を読めるので、秘密情報の置き場所を塞ぐ
                "denyRead": [str(p) for p in config.deny_read],
            },
        },
        "permissions": claude_permissions(config, ws, policy),
    }


def strip_env(base: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in base.items() if k in KEPT_CLAUDE_ENV or not (
        k.startswith(STRIPPED_ENV_PREFIXES) or _KEI_AGENT_SECRET_ENV.match(k))}


# Kei Agent 自身の差分の確認（docs/architecture.md）

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    return [line for line in _git(repo, "diff", "--name-only", f"{base}..{head}").splitlines() if line]


def touches_protected(files: list[str]) -> list[str]:
    return [f for f in files if any(f == p or f.startswith(p) for p in PROTECTED_PATHS)]


def touches_dependencies(files: list[str]) -> list[str]:
    return [f for f in files if f in DEPENDENCY_PATHS]


def check_change(repo: Path, base: str, head: str) -> list[str]:
    """取り込んでよい差分か。困るところがあれば、その理由を並べて返す（空なら問題なし）。"""
    problems = []
    files = changed_files(repo, base, head)
    if not files:
        problems.append("変わったファイルがありません")
    protected = touches_protected(files)
    if protected:
        problems.append("柵のファイルに触れています: " + ", ".join(protected))
    diff = _git(repo, "diff", f"{base}..{head}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(diff):
            problems.append("秘密情報らしい文字列が差分に入っています")
            break
    for line in _git(repo, "diff", "--numstat", f"{base}..{head}").splitlines():
        added, _, name = (line.split("\t", 2) + ["", "", ""])[:3]
        if added == "-":
            problems.append(f"テキストでないファイルが入っています: {name}")
        elif added.isdigit() and int(added) * 80 > MAX_DIFF_BYTES:
            problems.append(f"1つのファイルの変更が大きすぎます: {name}（{added} 行）")
    return problems
