"""Slack から Kei Agent 自身を直す流れ（docs/architecture.md）。

`#00_kei-agent` のスレッドで案を決め、手元の git worktree で直し、確認を通ってから
main に取り込んで push し、作業がなくなってから自分を再起動する。
柵（`guard.py`、`config.toml`、`deploy/`）に触れた差分は取り込まない。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kei_agent import guard, issues
from kei_agent.config import Config
from kei_agent.store import Store

log = logging.getLogger(__name__)

# Claude が返答の最後に書く行。Kei Agent 本体は、依頼者の投稿で始まった回の返事にあるときだけ動く
START_MARKER = "🛠 着手"
# 直したあと、コミットの件名として書いてもらう行
SUBJECT_MARKER = "📝 件名:"
MERGE_MARKER = "📦 取り込み"

# 取り込んだあとに残すファイル。新しい版が Slack につながったら消す
PENDING_NAME = "update-pending"
# deploy/run.sh が、起動できずに戻したときに残すファイル
ROLLED_BACK_NAME = "update-rolled-back"

# 要望の古い控え（`overview/` の中）。issue に移したら人が消す
BACKLOG_NAME = "backlog.md"
# 移すときの控え（要約と、作った issue の番号）。同じ要望を二度 issue にしない
MIGRATION_NAME = "backlog-issues.json"
_BACKLOG_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} ")
_BACKLOG_LINK = re.compile(r"\s*（\[Slack\]\([^)]*\)）\s*$")


# 案への「いいよ」と、「これで進めていい？」への「いいよ」の2回。これを数えてから着手する
REPLIES_BEFORE_START = 2


def wants(text: str, marker: str) -> bool:
    return any(line.strip().startswith(marker) for line in text.splitlines())


def strip_markers(text: str) -> str:
    """合図の行（着手・取り込み）を、Slack に出す本文から取り除く。"""
    kept = [line for line in text.splitlines()
            if not line.strip().startswith((START_MARKER, MERGE_MARKER))]
    return "\n".join(kept).rstrip()


def owner_replies(messages: list[dict], bot_user_id: str, thread_ts: str, current_ts: str | None = None) -> int:
    """スレッドで依頼者が返事した回数（最初の依頼は数えない）。いま届いた返事も数える。"""
    seen = {m.get("ts") for m in messages
            if m.get("ts") != thread_ts and not m.get("bot_id") and m.get("user") != bot_user_id}
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


def worktree_root(config: Config) -> Path:
    return config.state_dir / "worktrees"


def create_worktree(config: Config, thread_ts: str) -> tuple[Path, str, str]:
    """直すための作業場所を作る。(場所, ブランチ名, 元のコミット) を返す。"""
    repo = config.repo_root
    branch = f"kei-agent/improve-{thread_ts.replace('.', '-')}"
    path = worktree_root(config) / branch.split("/")[-1]
    remove_worktree(config, path, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = head(repo)
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path, branch, base


def remove_worktree(config: Config, path: Path, branch: str) -> None:
    repo = config.repo_root
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)
    git(repo, "branch", "-D", branch, check=False)


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


# エージェントのテストを飛ばさないために、確認で入れる依存のグループ
AGENT_GROUPS = ("--group", "course", "--group", "research", "--group", "work")


def run_checks(worktree: Path) -> CommandResult:
    """テストと ruff。依頼者が差分を見て「いいよ」と言ったあとに、sandbox の外で動かす。

    エージェント（大学・研究・仕事）のテストは、依存のグループを入れないと黙って飛ばされるので、
    ここで全部のグループを指定する（docs/architecture.md の「振り分けと A2A」）。
    """
    outputs = []
    pytest_args = ["uv", "run", "--frozen", *AGENT_GROUPS, "pytest", "-q"]
    for args in (pytest_args, ["uvx", "ruff", "check", "src", "tests", "plugin"]):
        proc = subprocess.run(args, cwd=worktree, capture_output=True, text=True, timeout=1800)
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        outputs.append(f"$ {' '.join(args)}\n{tail}")
        if proc.returncode != 0:
            return CommandResult(False, "\n\n".join(outputs))
    return CommandResult(True, "\n\n".join(outputs))


def restart_agents(config: Config) -> list[str]:
    """エージェント（別プロセス）を、新しい版で起動し直す。

    本体は launchd が入れ替えるが、エージェントは動き続けてしまう（古いコードのまま）。
    取り込んだあと、本体が静かになってから呼ぶ。
    """
    done = []
    for name in config.a2a.agents:
        label = f"com.kei-agent.{name}"
        proc = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            done.append(name)
        else:
            log.warning("%s を起動し直せません: %s", label, (proc.stderr or "").strip()[:200])
    if done:
        log.info("エージェントを起動し直しました: %s", "、".join(done))
    return done


class PushError(RuntimeError):
    """取り込んだが push できなかった。`undone` は手元の main を元に戻せたか。"""

    def __init__(self, output: str, undone: bool):
        super().__init__(output)
        self.output = output
        self.undone = undone


def merge_and_push(config: Config, branch: str) -> str:
    """main に早送りで取り込み、GitHub に push する。取り込んだコミットを返す。

    push できなければ、手元の main を取り込む前に戻して PushError にする
    （手元だけ進んで GitHub とずれたまま再起動しない）。
    """
    repo = config.repo_root
    base = head(repo)
    git(repo, "merge", "--ff-only", branch)
    merged = head(repo)
    pushed = git(repo, "push", "origin", "main", check=False)
    if not pushed.ok:
        undone = git(repo, "reset", "--keep", base, check=False).ok
        raise PushError(pushed.output[:1000], undone)
    return merged


def diff_text(repo: Path, base: str, ref: str) -> str:
    return git(repo, "diff", f"{base}..{ref}").output


def review_summary(config: Config, worktree: Path, base: str, summary: str, checks: str) -> str:
    """取り込む前に見せる文。依存ライブラリの変更は、いちばん上に出す。"""
    files = guard.changed_files(worktree, base, "HEAD")
    lines = []
    deps = guard.touches_dependencies(files)
    if deps:
        lines.append(f"⚠️ 依存するライブラリが変わる（{', '.join(deps)}）。中身を確かめてね")
    lines.append(summary.strip())
    lines.append("")
    lines.append("*変えたファイル*")
    lines += [f"• `{f}`" for f in files] or ["• なし"]
    if checks:
        lines += ["", "*Claude が sandbox の中で回した確認*", checks.strip()]
    lines += ["", "取り込んでいい？（柵のファイルに触れていないことは確認済み。テストは取り込む前にもう一度回す）"]
    return "\n".join(lines)


def pending_path(config: Config) -> Path:
    return config.state_dir / PENDING_NAME


def rolled_back_path(config: Config) -> Path:
    return config.state_dir / ROLLED_BACK_NAME


def mark_pending(config: Config, previous: str, thread_ts: str) -> None:
    """再起動の前に残す。新しい版が Slack につながったら消す。残り続けたら run.sh が前の版に戻す。"""
    pending_path(config).write_text(f"{previous}\n0\n{thread_ts}\n", encoding="utf-8")


def _read_marker(path: Path) -> tuple[str, str] | None:
    """update-pending / update-rolled-back の (前のコミット, スレッド)。3行目がスレッド。"""
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    return (lines[0] if lines else "", lines[2] if len(lines) > 2 else "")


def read_pending(config: Config) -> tuple[str, str] | None:
    return _read_marker(pending_path(config))


def read_rolled_back(config: Config) -> tuple[str, str] | None:
    return _read_marker(rolled_back_path(config))


FIX_PROMPT = """\
[Kei Agent からの自動メッセージ] このスレッドで決まった直し方で、Kei Agent 自身のコードを直してください。

- いまのディレクトリは、この作業のための git worktree です。ここの中だけを書き換えます
- `src/kei_agent/guard.py`、`config.toml`、`deploy/` は触らないでください（柵なので、触れた差分は捨てられます）
- 直したら `uv run --frozen pytest -q` と `uvx ruff check src tests plugin` を通してください
- テストのないところを直すときは、先に落ちるテストを書いてから直してください
- コミットはしないでください（Kei Agent 本体がまとめてコミットします）
- 最後に、何をどう変えたかと、テストの結果を短くまとめてください
- いちばん最後の行に `📝 件名: <コミットの件名を一行で>` と書いてください（何をしたかが分かる、50字くらいの日本語）

これまでのやりとり:
"""


def push_revert(config: Config) -> None:
    """run.sh が戻した取り消しを GitHub にも送る。"""
    git(config.repo_root, "push", "origin", "main", check=False)


def backlog_requests(path: Path) -> list[str]:
    """backlog.md のまだ済んでいない要望（`- [ ]`）の文。日時と Slack へのリンクは外す。"""
    items: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- [ ] "):
            current = [line.removeprefix("- [ ] ")]
            items.append(current)
        elif current is not None and line.startswith("  "):
            current.append(line[2:])
        else:
            current = None
    texts = (_BACKLOG_LINK.sub("", _BACKLOG_STAMP.sub("", "\n".join(lines))).strip() for lines in items)
    return [text for text in texts if text]


def migrate_backlog_to_issues(config: Config, dry_run: bool = True) -> list[dict]:
    """一度だけ使う: `overview/backlog.md` のまだ済んでいない要望を、要約した GitHub issue にする。

    dry_run では要約を作って返すだけ（`<state_dir>/backlog-issues.json` に控える）。dry_run=False では
    控えた要約（なければ新しく要約）で issue を作り、番号を返す。作れたものは、もう一度呼んでも作らない。
    backlog.md は消さない。返す要望の原文は手元で確かめるためのもので、issue には載せない。

        uv run python -c 'import json; from kei_agent import config, improve; print(json.dumps(
            improve.migrate_backlog_to_issues(config.load_config()), ensure_ascii=False, indent=2))'
    """
    return asyncio.run(_migrate_backlog(config, dry_run))


async def _migrate_backlog(config: Config, dry_run: bool) -> list[dict]:
    path = config.overview_dir / BACKLOG_NAME
    if not path.exists():
        return []
    saved_path = config.state_dir / MIGRATION_NAME
    # 要望の原文は控えに書かない（鍵は原文のハッシュ）
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.exists() else {}
    store = Store(config.db_path)
    results = []
    try:
        for text in backlog_requests(path):
            key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            entry = {k: v for k, v in saved.get(key, {}).items() if k != "error"}
            if not entry.get("number"):
                try:
                    if entry.get("title"):
                        # 見て確かめた（手で直したかもしれない）要約。もう一度確かめてから使う
                        summary = issues.Summary(entry["title"], entry.get("body", ""))
                        if found := issues.problems(summary, text):
                            raise issues.IssueError("控えの要約が公開の条件に合いません", "、".join(found))
                    else:
                        summary = await issues.summarize(config, store, text)
                    entry.update(title=summary.title, body=summary.body)
                    if not dry_run:
                        entry["number"] = (await issues.create(config, summary)).number
                except issues.IssueError as e:
                    entry["error"] = f"{e.reason}（{e.detail}）" if e.detail else e.reason
                saved[key] = entry
                saved_path.parent.mkdir(parents=True, exist_ok=True)
                saved_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append({"request": text, **entry})
    finally:
        store.conn.close()
    return results


def subject_from(text: str, fallback: str) -> str:
    """Claude が書いた `📝 件名:` の行。なければ要望の先頭を使う。"""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(SUBJECT_MARKER):
            subject = line[len(SUBJECT_MARKER):].strip()
            if subject:
                return subject[:72]
    return " ".join(fallback.split())[:50]


def commit_message(request: str, summary: str) -> str:
    """Kei Agent 自身を直したときのコミットメッセージ。件名は Claude が書いた1行、本文は変えた内容の要約。"""
    body = [line for line in summary.strip().splitlines() if not line.strip().startswith(SUBJECT_MARKER)]
    text = "\n".join(body).strip()[:1500]
    return f"{subject_from(summary, request)}\n\n{text}\n\n#00_kei-agent の要望から、Kei Agent 自身が直した。"
