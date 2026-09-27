"""Kei Agent 自身のリポジトリを直す部品（git の操作・確認・合図）。本体の窓口には触れず、場所は引数で受け取る。

直すのは、状態の置き場の modules/improve/worktrees/ の下の worktree。main への取り込みは早送りだけで、push できなければ
手元の main を元に戻す。柵の確認は本体（core.check_change）が行う。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# AI が返答の最後に書く行。依頼者の投稿で始まった回の返事にあるときだけ動く
START_MARKER = "🛠 着手"
MERGE_MARKER = "📦 取り込み"
# 直したあと、コミットの件名として書いてもらう行
SUBJECT_MARKER = "📝 件名:"
# 案への「いいよ」と、「これで進めていい？」への「いいよ」の2回。これを数えてから着手する
REPLIES_BEFORE_START = 2
# 依存するライブラリの定義。変わるときは、取り込む前の文のいちばん上に出す
DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock")
# エージェントのテストを飛ばさないために、確認で入れる依存のグループ
AGENT_GROUPS = ("--group", "agents")
# 確認（テストと ruff）の上限時間（秒）
CHECK_TIMEOUT_SECONDS = 1800


def wants(text: str, marker: str) -> bool:
    return any(line.strip().startswith(marker) for line in text.splitlines())


def owner_replies(messages: list[dict], is_owner: Callable[[str], bool], thread_ts: str,
                  current_ts: str | None = None) -> int:
    """スレッドで依頼者が返事した回数（最初の依頼は数えない）。いま届いた返事も数える。"""
    seen = {m.get("ts") for m in messages
            if m.get("ts") != thread_ts and not m.get("bot_id") and is_owner(str(m.get("user") or ""))}
    if current_ts and current_ts != thread_ts:
        seen.add(current_ts)
    return len(seen)


@dataclass
class CommandResult:
    ok: bool
    output: str


def git(repo: Path, *args: str, check: bool = True) -> CommandResult:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()[:500]}")
    return CommandResult(proc.returncode == 0, (proc.stdout + proc.stderr).strip())


def head(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).output


def repo_dirty(repo: Path) -> bool:
    """コミットしていない変更があるか（追跡していないファイルは数えない）。"""
    return bool(git(repo, "status", "--porcelain", "--untracked-files=no").output)


def branch_of(thread_ts: str) -> str:
    return f"kei-agent/improve-{thread_ts.replace('.', '-')}"


def create_worktree(repo: Path, root: Path, thread_ts: str) -> tuple[Path, str, str]:
    """直すための作業場所を root の下に作る。(場所, ブランチ名, 元のコミット) を返す。"""
    branch = branch_of(thread_ts)
    path = root / branch.split("/")[-1]
    remove_worktree(repo, path, branch)
    root.mkdir(parents=True, exist_ok=True)
    base = head(repo)
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path, branch, base


def remove_worktree(repo: Path, path: Path, branch: str) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)
    git(repo, "branch", "-D", branch, check=False)


def clean(repo: Path, worktrees: Path, talks: Path, keep_worktrees: set[str], keep_talks: set[str]) -> int:
    """使い終わった worktree と、相談の作業用のフォルダを片づける。片づけた worktree の数を返す。"""
    removed = 0
    if worktrees.is_dir():
        for path in sorted(p for p in worktrees.iterdir() if p.is_dir()):
            if path.name in keep_worktrees:
                continue
            remove_worktree(repo, path, f"kei-agent/{path.name}")
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if talks.is_dir():
        for path in sorted(p for p in talks.iterdir() if p.is_dir()):
            if path.name not in keep_talks:
                shutil.rmtree(path, ignore_errors=True)
    return removed


def commit_all(worktree: Path, message: str) -> str | None:
    """worktree の変更をまとめてコミットする。変更がなければ None。"""
    git(worktree, "add", "-A")
    if not git(worktree, "status", "--porcelain").output:
        return None
    git(worktree, "commit", "-q", "-m", message)
    return head(worktree)


def catch_up_with_main(worktree: Path) -> CommandResult:
    """main が先に進んでいたら、その上に乗せ直す。"""
    result = git(worktree, "rebase", "main", check=False)
    if not result.ok:
        git(worktree, "rebase", "--abort", check=False)
    return result


def run_checks(worktree: Path) -> CommandResult:
    """テストと ruff。依頼者が差分を見て「いいよ」と言ったあとに、sandbox の外で動かす。

    エージェント（担当のプロセス）のテストは、依存のグループを入れないと黙って飛ばされるので、ここで指定する。
    """
    outputs = []
    pytest_args = ["uv", "run", "--frozen", *AGENT_GROUPS, "pytest", "-q"]
    for args in (pytest_args, ["uvx", "ruff", "check", "src", "tests", "modules"]):
        proc = subprocess.run(args, cwd=worktree, capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS)
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        outputs.append(f"$ {' '.join(args)}\n{tail}")
        if proc.returncode != 0:
            return CommandResult(False, "\n\n".join(outputs))
    return CommandResult(True, "\n\n".join(outputs))


class PushError(RuntimeError):
    """取り込んだが push できなかった。`undone` は手元の main を元に戻せたか。"""

    def __init__(self, output: str, undone: bool):
        super().__init__(output)
        self.output = output
        self.undone = undone


def merge_and_push(repo: Path, branch: str) -> str:
    """main に早送りで取り込み、GitHub に push する。取り込んだコミットを返す。

    push できなければ、手元の main を取り込む前に戻して PushError にする（手元だけ進んで GitHub とずれたまま再起動しない）。
    """
    base = head(repo)
    git(repo, "merge", "--ff-only", branch)
    merged = head(repo)
    pushed = git(repo, "push", "origin", "main", check=False)
    if not pushed.ok:
        undone = git(repo, "reset", "--keep", base, check=False).ok
        raise PushError(pushed.output[:1000], undone)
    return merged


def push_revert(repo: Path) -> None:
    """本体（deploy/run.sh）が前の版に戻した取り消しを、GitHub にも送る。"""
    git(repo, "push", "origin", "main", check=False)


def diff_text(repo: Path, base: str, ref: str = "HEAD") -> str:
    return git(repo, "diff", f"{base}..{ref}").output


def changed_files(repo: Path, base: str, ref: str = "HEAD") -> list[str]:
    return [line for line in git(repo, "diff", "--name-only", f"{base}..{ref}").output.splitlines() if line]


def review_summary(worktree: Path, base: str, summary: str) -> str:
    """取り込む前に見せる文。依存ライブラリの変更は、いちばん上に出す。"""
    files = changed_files(worktree, base)
    lines = []
    deps = [f for f in files if f in DEPENDENCY_PATHS]
    if deps:
        lines.append(f"⚠️ 依存するライブラリが変わる（{', '.join(deps)}）。中身を確かめてね")
    lines += [summary.strip(), "", "*変えたファイル*"]
    lines += [f"• `{f}`" for f in files] or ["• なし"]
    lines += ["", "取り込んでいい？（柵のファイルに触れていないことは確認済み。テストは取り込む前にもう一度回す）"]
    return "\n".join(lines)


FIX_PROMPT = """\
[Kei Agent からの自動メッセージ] このスレッドで決まった直し方で、Kei Agent 自身のコードを直してください。

- いまのディレクトリは、この作業のための git worktree です。ここの中だけを書き換えます
- `src/kei_agent/guard.py`、`config.example.toml`、`deploy/` は触らないでください（柵なので、触れた差分は捨てられます）
- 直したら `uv run --frozen --group agents pytest -q` と `uvx ruff check src tests modules` を通してください
- テストのないところを直すときは、先に落ちるテストを書いてから直してください
- コミットはしないでください（Kei Agent 本体がまとめてコミットします）
- 最後に、何をどう変えたかと、テストの結果を短くまとめてください
- いちばん最後の行に `📝 件名: <コミットの件名を一行で>` と書いてください（何をしたかが分かる、50字くらいの日本語）

これまでのやりとり:
"""


def subject_from(text: str, fallback: str) -> str:
    """AI が書いた `📝 件名:` の行。なければ要望の先頭を使う。"""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(SUBJECT_MARKER):
            subject = line[len(SUBJECT_MARKER):].strip()
            if subject:
                return subject[:72]
    return " ".join(fallback.split())[:50]


def commit_message(request: str, summary: str) -> str:
    """Kei Agent 自身を直したときのコミットメッセージ。件名は AI が書いた1行、本文は変えた内容の要約。"""
    body = [line for line in summary.strip().splitlines() if not line.strip().startswith(SUBJECT_MARKER)]
    text = "\n".join(body).strip()[:1500]
    return f"{subject_from(summary, request)}\n\n{text}\n\n#00_kei-agent の要望から、Kei Agent 自身が直した。"
