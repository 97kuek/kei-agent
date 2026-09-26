"""要望を、要約した公開の GitHub issue にする（docs/architecture.md の「自己改善」）。

リポジトリは公開なので、issue には軽いモデルが一般化した要約だけを載せる。Slack の文・URL・
ワークスペースやチャンネルや人の名前・手元のパス・秘密情報は載せず、原文は Slack に残す。
GitHub には `gh` CLI で書く（`gh auth login` 済みの前提）。テストでは conftest が `gh` と `summarize` を差し替える。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from kei_agent import runner
from kei_agent.config import Config
from kei_agent.guard import SECRET_PATTERNS
from kei_agent.model_policy import PROVIDERS, ModelPolicyError, UseCase, resolve
from kei_agent.router import workspace
from kei_agent.settings import selected_provider

# issue に付けるラベル。無ければ作る
LABEL = "kei-agent-request"
LABEL_COLOR = "1D76DB"
LABEL_DESCRIPTION = "Slack で受けた Kei Agent への要望（要約）"
TITLE_LIMIT = 60
BODY_LINES = 4
LINE_LIMIT = 120
# 原文とこれだけ続けて同じなら、写したとみなす（空白・全角半角・大文字小文字の違いは見ない）
OVERLAP = 20
# 要約に渡す要望の長さ
TEXT_LIMIT = 2000
GH_TIMEOUT_SECONDS = 60

PROMPT = """次の、Kei Agent（Slack で動く個人用アシスタント）への要望を、公開リポジトリの GitHub issue 用に一般化して要約してください。
- 日本語。title は{title}字以内の1行、body は1〜{lines}行の短い箇条書き
- 要望の文を写さず、自分の言葉で書く
- URL、ワークスペース・チャンネル・人の名前、手元のパス、秘密情報は書かない
JSON 1行だけで答えてください。形: {{"title": "…", "body": "…"}}（body の改行は \\n）

要望:
{text}"""

_URL = re.compile(r"://|www\.|slack\.com", re.IGNORECASE)
_LOCAL_PATHS = ("/Users/", "~/")
# Slack のメンション・チャンネル（`<@U…>` `<#C…>` `<!here>`）と、GitHub で人に届く `@name`・メールアドレス
_MENTION = re.compile(r"[@＠]|<[#!]")
# `#00_kei-agent` のようなチャンネル名（全角の＃も）
_CHANNEL = re.compile(r"[#＃][^\s#＃]")
# ひらがな・カタカナ・漢字
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_GITHUB = re.compile(r"^(?:https://(?:[^@/\s]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])"
                     r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_ISSUE_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/(\d+)")


class IssueError(RuntimeError):
    """issue を作れない・閉じられない。`reason` は Slack に出してよい短い理由、`detail` はログにだけ残す。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class NoProvider(IssueError):
    """自己改善の AI（Claude か Codex）が選ばれていない。"""


@dataclass(frozen=True)
class Summary:
    title: str
    body: str


@dataclass(frozen=True)
class Issue:
    number: int
    url: str


# 要約

def parse(text: str) -> Summary | None:
    """モデルの返事から {"title", "body"} を拾う（前後に文が付いていてもよい）。読めなければ None。"""
    start = (text or "").find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    title, body = data.get("title"), data.get("body")
    if isinstance(body, list) and all(isinstance(line, str) for line in body):
        body = "\n".join(body)
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    return Summary(title.strip(), body.strip())


def _squash(text: str) -> str:
    """写しを見つけるための形。空白・全角半角・大文字小文字の違いを消す。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def _copies(text: str, original: str) -> bool:
    """原文と OVERLAP 字以上続けて同じところがあるか。"""
    source, target = _squash(original), _squash(text)
    pieces = {source[i:i + OVERLAP] for i in range(len(source) - OVERLAP + 1)}
    return any(target[i:i + OVERLAP] in pieces for i in range(len(target) - OVERLAP + 1))


def problems(summary: Summary, original: str) -> list[str]:
    """公開してよい要約かを確かめる。だめな理由を返す（理由に要約や原文の中身は入れない）。"""
    found = []
    lines = [line for line in summary.body.splitlines() if line.strip()]
    text = f"{summary.title}\n{summary.body}"
    if not summary.title or "\n" in summary.title or len(summary.title) > TITLE_LIMIT:
        found.append(f"題が1行・{TITLE_LIMIT}字以内でない")
    if not 1 <= len(lines) <= BODY_LINES or any(len(line) > LINE_LIMIT for line in lines):
        found.append(f"本文が1〜{BODY_LINES}行の短い文でない")
    if not (_JAPANESE.search(summary.title) and _JAPANESE.search(summary.body)):
        found.append("日本語でない")
    if _URL.search(text):
        found.append("URL を含む")
    if any(path in text for path in _LOCAL_PATHS):
        found.append("手元のパスを含む")
    if _MENTION.search(text) or _CHANNEL.search(text):
        found.append("メンションかチャンネル名を含む")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        found.append("秘密情報らしい文字列を含む")
    if _copies(text, original):
        found.append(f"原文と{OVERLAP}字以上同じ")
    return found


async def summarize(config: Config, store, text: str) -> Summary:
    """要望を、自己改善で選んだ provider の軽い recipe（routing）で1回だけ要約する。

    作業場は分類と同じ read-only の一時ディレクトリ。条件に合わない要約は直さずに断る（issue は作らない）。
    """
    provider = selected_provider(config, store, "self_fix")
    if provider not in PROVIDERS:
        raise NoProvider("自己改善の AI（Claude か Codex）が選ばれていません")
    try:
        # 振り分けと同じ軽い recipe を、自己改善で選んだ provider で使う
        recipe = resolve("router", provider, UseCase.ROUTING)
    except ModelPolicyError as e:
        raise IssueError("要約の recipe を決められません", str(e)) from e
    prompt = PROMPT.format(title=TITLE_LIMIT, lines=BODY_LINES, text=text.strip()[:TEXT_LIMIT])
    try:
        result = await runner.run_model(
            config, runner.ExecutionRequest(workspace(config, "self_fix"), recipe, None, "", "", read_only=True),
            prompt,
        )
    except Exception as e:
        raise IssueError("要約の AI を動かせません", f"{type(e).__name__}: {e}") from e
    if result.limit_reset_at is not None:
        raise IssueError("要約の AI が利用上限に達しています")
    if result.is_error:
        raise IssueError("要約の AI が失敗しました", result.failure_reason(500))
    summary = parse(result.text)
    if summary is None:
        raise IssueError("要約が JSON になっていません")
    found = problems(summary, text)
    if found:
        raise IssueError("要約が公開の条件に合いません", "、".join(found))
    return summary


# GitHub（gh）

def parse_slug(url: str) -> str | None:
    """origin の URL から `owner/name`。GitHub でなければ None。"""
    found = _GITHUB.match(url.strip())
    return f"{found.group(1)}/{found.group(2)}" if found else None


async def _run(*command: str) -> tuple[int, str, str]:
    """コマンドを1回動かす。見つからない・終わらないときは IssueError。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # launchd の下では対話の確認に答えられない
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
        )
    except FileNotFoundError as e:
        raise IssueError(f"{command[0]} が見つかりません") from e
    except OSError as e:
        raise IssueError(f"{command[0]} を起動できません", str(e)) from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GH_TIMEOUT_SECONDS)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise IssueError(f"{command[0]} が時間内に終わりません") from e
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def repo_slug(repo_root: Path) -> str | None:
    """Kei Agent のリポジトリ（origin）の `owner/name`。URL は鍵を含みうるので、どこにも出さない。"""
    code, out, _ = await _run("git", "-C", str(repo_root), "remote", "get-url", "origin")
    return parse_slug(out) if code == 0 else None


async def run_gh(*args: str) -> str:
    """gh を1回動かして標準出力を返す。失敗の中身は detail に入れる（ログにだけ残す）。"""
    code, out, err = await _run("gh", *args)
    if code != 0:
        raise IssueError("gh が失敗しました", (err or out).strip()[:500])
    return out


async def gh(config: Config, *args: str) -> str:
    """Kei Agent のリポジトリに対して gh を動かす。"""
    repo = await repo_slug(config.repo_root)
    if repo is None:
        raise IssueError("origin が GitHub のリポジトリではありません")
    return await run_gh(*args, "--repo", repo)


async def ensure_label(config: Config) -> None:
    """issue に付けるラベルを用意する。もうあれば gh が already exists で断るだけなので、それは成功とみなす。"""
    try:
        await gh(config, "label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION)
    except IssueError as e:
        if "already exists" not in e.detail:
            raise


async def create(config: Config, summary: Summary) -> Issue:
    """要約だけで issue を作る（ラベル付き）。"""
    await ensure_label(config)
    out = await gh(config, "issue", "create", "--title", summary.title, "--body", summary.body, "--label", LABEL)
    found = _ISSUE_URL.search(out)
    if found is None:
        raise IssueError("作った issue の番号が分かりません", out.strip()[:200])
    return Issue(int(found.group(1)), found.group(0))


async def close(config: Config, number: int, commit: str) -> None:
    """取り込んだ要望の issue を、取り込んだコミットの短い sha を添えて閉じる。"""
    comment = f"{commit[:7]} で取り込みました。" if commit else "取り込みました。"
    await gh(config, "issue", "close", str(number), "--comment", comment)
