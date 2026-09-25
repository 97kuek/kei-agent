"""ジョブ: テーマのディレクトリに置かれた依頼を読み、pueue で走らせ、状態を追う。

Claude からは skill のスクリプト（plugin/research/skills/running-jobs/scripts/kei_agent_job.py）で
`.kei-agent/requests/*.json` に依頼を書くだけにし、検証と投入はここで行う。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kei_agent import themes
from kei_agent.config import Config, path_without_venv
from kei_agent.store import Job, Store, dumps

log = logging.getLogger(__name__)

PUEUE_GROUP = "kei-agent"
REQUESTS_DIR = Path(".kei-agent/requests")
JOBS_DIR = Path(".kei-agent/jobs")
# ジョブのログの末尾を読むとき、読み込む最大の大きさ
LOG_TAIL_BYTES = 64 * 1024
# 書きかけのまま残った依頼のファイルを消すまでの秒数
TMP_LIFETIME_SECONDS = 3600
# pueue_id が空のまま、これより古い行は、投入の途中で止まったものとみなす
SUBMIT_GRACE_SECONDS = 300
# 1つのジョブで宣言できる「できるはずのファイル」の数
MAX_EXPECTED_FILES = 10
# ジョブに渡す環境変数。pueue は投入したプロセスの環境をそのまま保存するので、Slack のトークンなどを持ち込まない
_JOB_ENV_KEYS = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL")


class JobRequestError(ValueError):
    pass


@dataclass(frozen=True)
class SubmitRequest:
    request_id: str
    channel: str
    thread_ts: str
    name: str
    script: str
    args: list[str]
    # ジョブが作るはずのファイル。終わったときに、本当にできたかを確かめる
    expects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    """依頼を1件処理した結果。スレッドに知らせるために返す。"""
    channel: str
    thread_ts: str
    job: Job | None = None
    error: str | None = None


@dataclass(frozen=True)
class CancelRequest:
    request_id: str
    job_id: int


def parse_request(data: dict) -> SubmitRequest | CancelRequest:
    action = data.get("action")
    request_id = str(data.get("request_id") or "")
    if not request_id:
        raise JobRequestError("request_id がありません")
    if action == "cancel":
        try:
            return CancelRequest(request_id, int(data["job_id"]))
        except (KeyError, TypeError, ValueError):
            raise JobRequestError("cancel には数字の job_id が要ります") from None
    if action != "submit":
        raise JobRequestError(f"不明な action: {action!r}")
    args = data.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise JobRequestError("args は文字列のリストにしてください")
    expects = data.get("expects") or []
    if not isinstance(expects, list) or not all(isinstance(e, str) for e in expects):
        raise JobRequestError("expects は文字列のリストにしてください")
    if len(expects) > MAX_EXPECTED_FILES:
        raise JobRequestError(f"expects は {MAX_EXPECTED_FILES} 個までにしてください")
    for e in expects:
        path = Path(e)
        if not e or path.is_absolute() or ".." in path.parts:
            raise JobRequestError(f"expects はテーマのディレクトリからの相対パスにしてください: {e or "（空）"}")
    return SubmitRequest(
        request_id=request_id,
        channel=str(data.get("channel") or ""),
        thread_ts=str(data.get("thread_ts") or ""),
        name=str(data.get("name") or "job")[:80],
        script=str(data.get("script") or ""),
        args=args,
        expects=[str(Path(e)) for e in expects],
    )


def build_job_command(cwd: Path, script: str, args: list[str], job_id: int) -> str:
    """テーマのディレクトリの中にあるスクリプトだけを、ログつきで実行するコマンドを作る。"""
    rel = Path(script)
    if not script or rel.is_absolute() or ".." in rel.parts:
        raise JobRequestError("script はテーマのディレクトリからの相対パスにしてください")
    root = cwd.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise JobRequestError("script がテーマのディレクトリの外を指しています")
    if not path.is_file():
        raise JobRequestError(f"script が見つかりません: {script}")

    rel_str = str(path.relative_to(root))
    if path.suffix == ".py":
        runner = ["uv", "run", "python", rel_str] if (root / "pyproject.toml").exists() else ["python3", rel_str]
    elif path.suffix == ".sh":
        runner = ["bash", rel_str]
    else:
        raise JobRequestError("script は .py か .sh にしてください")

    quoted = " ".join(shlex.quote(a) for a in [*runner, *args])
    log_path = f"logs/job-{job_id}.log"
    return f"mkdir -p logs && exec {quoted} > {log_path} 2>&1"


def job_env(base: dict[str, str], repo_root: Path) -> dict[str, str]:
    env = {k: base[k] for k in _JOB_ENV_KEYS if k in base}
    env["PATH"] = path_without_venv(base.get("PATH", "/usr/bin:/bin"), repo_root)
    return env


def _parse_time(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


def interpret_pueue_status(task: dict) -> tuple[str, dict]:
    """pueue の task から (Kei Agent の状態, 追加で保存する値) を作る。"""
    status = task.get("status")
    if isinstance(status, str):
        # 中身を持たない形で返る状態もある
        return ("running", {}) if status == "Running" else ("queued", {})
    (name, body), = status.items()
    body = body or {}
    if name == "Running":
        return "running", {"started_at": _parse_time(body.get("start"))}
    if name in ("Queued", "Stashed", "Paused", "Locked"):
        return "queued", {}
    if name == "Done":
        result = body.get("result")
        times = {"started_at": _parse_time(body.get("start")), "finished_at": _parse_time(body.get("end"))}
        if result == "Success":
            return "succeeded", times
        if result == "Killed":
            return "cancelled", {**times, "detail": "取り消されました"}
        if isinstance(result, dict) and "Failed" in result:
            return "failed", {**times, "detail": f"終了コード {result['Failed']}"}
        return "failed", {**times, "detail": f"失敗しました: {result}"}
    return "queued", {}


class Pueue:
    def __init__(self, config: Config):
        self.bin = config.pueue_bin
        self.parallel = config.job_parallel
        self.repo_root = config.repo_root

    async def _run(self, *args: str, env: dict[str, str] | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.bin, *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"pueue {' '.join(args)}: {err.decode().strip()}")
        return out.decode()

    async def ensure_group(self) -> None:
        status = json.loads(await self._run("status", "--json"))
        if PUEUE_GROUP not in status.get("groups", {}):
            await self._run("group", "add", PUEUE_GROUP)
        await self._run("parallel", str(self.parallel), "--group", PUEUE_GROUP)

    async def add(self, cwd: Path, command: str, label: str) -> int:
        out = await self._run(
            "add", "--group", PUEUE_GROUP, "--working-directory", str(cwd),
            "--label", label, "--print-task-id", "--", command,
            env=job_env(dict(os.environ), self.repo_root),
        )
        return int(out.strip())

    async def kill(self, task_id: int) -> None:
        await self._run("kill", str(task_id))

    async def remove(self, task_id: int) -> None:
        await self._run("remove", str(task_id))

    async def tasks(self) -> dict[int, dict]:
        status = json.loads(await self._run("status", "--json", "--group", PUEUE_GROUP))
        return {int(k): v for k, v in status.get("tasks", {}).items()}


class JobManager:
    def __init__(self, config: Config, store: Store, pueue: Pueue):
        self.config = config
        self.store = store
        self.pueue = pueue

    def _write_state(self, job: Job) -> None:
        jobs_dir = Path(job.cwd) / JOBS_DIR
        jobs_dir.mkdir(parents=True, exist_ok=True)
        (jobs_dir / f"{job.id}.json").write_text(dumps(job.to_state()), encoding="utf-8")

    async def process_requests(self, cwd: Path) -> list[Outcome]:
        """テーマのディレクトリに置かれた依頼を処理する。知らせることがある分だけ返す。"""
        requests_dir = cwd / REQUESTS_DIR
        if not requests_dir.is_dir():
            return []
        outcomes = []
        # 書き込みの途中で claude が落ちると `.xxx.tmp` が残る。誰も消さないので、ここで片づける
        for leftover in requests_dir.glob(".*.tmp"):
            if time.time() - leftover.stat().st_mtime > TMP_LIFETIME_SECONDS:
                leftover.unlink(missing_ok=True)
        for path in sorted(requests_dir.glob("*.json")):
            outcome = await self._handle_request(path, cwd)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    async def _handle_request(self, path: Path, cwd: Path) -> Outcome | None:
        """依頼を1件処理して、ファイルを消す。どの道を通っても消すことで、同じ失敗を繰り返さない。"""
        data: dict = {}
        try:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = raw if isinstance(raw, dict) else {}
                req = parse_request(raw if isinstance(raw, dict) else {})
            except (OSError, json.JSONDecodeError, JobRequestError, TypeError, ValueError) as e:
                # 読めない依頼を黙って捨てると、Claude は「投入した」と思ったまま待ち続ける
                log.warning("ジョブの依頼を読めません %s: %s", path, e)
                return Outcome(
                    str(data.get("channel") or ""), str(data.get("thread_ts") or ""),
                    error=f"ジョブの依頼（`{path.name}`）を読めませんでした: {e}",
                )
            if isinstance(req, CancelRequest):
                await self._cancel(req, cwd)
                return None
            if self.store.has_request(req.request_id):
                return None
            return await self._submit(req, cwd)
        finally:
            path.unlink(missing_ok=True)

    async def _submit(self, req: SubmitRequest, cwd: Path) -> Outcome:
        # 投入と pueue_id の記録の間で落ちても、ラベル（kei-agent-<id>）から refresh で拾い直す
        job = self.store.add_job(req.request_id, req.channel, req.thread_ts, str(cwd), req.name,
                                 f"{req.script} {' '.join(req.args)}".strip(),
                                 status="queued", expects=req.expects)
        try:
            self._check_thread(req, cwd)
            command = build_job_command(cwd, req.script, req.args, job.id)
        except JobRequestError as e:
            job = self.store.update_job(job.id, status="rejected", detail=str(e), reported=1)
            self._write_state(job)
            return Outcome(req.channel, req.thread_ts, job, str(e))
        try:
            task_id = await self.pueue.add(cwd, command, label=f"kei-agent-{job.id}")
        except RuntimeError as e:
            job = self.store.update_job(job.id, status="rejected", detail=f"pueue に投入できませんでした: {e}", reported=1)
            self._write_state(job)
            return Outcome(req.channel, req.thread_ts, job, job.detail)
        job = self.store.update_job(job.id, pueue_id=task_id)
        self._write_state(job)
        return Outcome(req.channel, req.thread_ts, job)

    def _check_thread(self, req: SubmitRequest, cwd: Path) -> None:
        """依頼に書かれたスレッドが、このテーマのディレクトリで動いているスレッドかを確かめる。"""
        row = self.store.get_thread(req.channel, req.thread_ts)
        if row is None:
            raise JobRequestError("依頼元のスレッドが見つかりません")
        ws = themes.resolve(self.config, row["channel_name"])
        if ws.cwd is None or ws.cwd.resolve() != cwd.resolve():
            raise JobRequestError("依頼元のスレッドとテーマのディレクトリが一致しません")

    async def _cancel(self, req: CancelRequest, cwd: Path) -> None:
        job = self.store.get_job(req.job_id)
        if job is None or Path(job.cwd) != cwd or job.pueue_id is None or job.status not in ("queued", "running"):
            return
        await self.pueue.kill(job.pueue_id)

    async def refresh(self) -> list[Job]:
        """pueue の状態を反映し、終わったばかりで未報告のジョブを返す。"""
        active = self.store.active_jobs()
        stuck = self.store.unsubmitted_jobs(time.time() - SUBMIT_GRACE_SECONDS)
        if active or stuck:
            tasks = await self.pueue.tasks()
            if stuck:
                self._recover_unsubmitted(stuck, tasks)
                active = self.store.active_jobs()
            for job in active:
                task = tasks.get(job.pueue_id)
                if task is None:
                    status, extra = "failed", {"detail": "pueue にタスクが見つかりません", "finished_at": time.time()}
                else:
                    status, extra = interpret_pueue_status(task)
                if status != job.status or extra.get("started_at", job.started_at) != job.started_at:
                    job = self.store.update_job(job.id, status=status, **extra)
                    self._write_state(job)
                    if status in ("succeeded", "failed", "cancelled") and task is not None:
                        try:
                            await self.pueue.remove(job.pueue_id)
                        except RuntimeError:
                            # 片づけに失敗しても、ジョブの状態はもう確定している
                            log.warning("pueue のタスク %s を片づけられません", job.pueue_id, exc_info=True)
        return self.store.unreported_finished_jobs()

    def _recover_unsubmitted(self, stuck: list[Job], tasks: dict[int, dict]) -> None:
        """pueue_id を書く前に止まったジョブ。pueue にあれば拾い直し、なければ失敗として知らせる。"""
        by_label = {task.get("label"): task_id for task_id, task in tasks.items() if task.get("label")}
        for job in stuck:
            task_id = by_label.get(f"kei-agent-{job.id}")
            if task_id is not None:
                log.warning("投入の途中で止まったジョブ %s を pueue のタスク %s として拾い直します", job.id, task_id)
                job = self.store.update_job(job.id, pueue_id=task_id)
            else:
                job = self.store.update_job(job.id, status="failed", finished_at=time.time(),
                                            detail="pueue に投入する途中で Kei Agent が止まりました")
            self._write_state(job)

    def mark_reported(self, job: Job) -> None:
        self.store.update_job(job.id, reported=1)


def missing_outputs(job: Job) -> list[str]:
    """宣言された「できるはずのファイル」のうち、無いか空のもの。

    pueue の終了コードは 0 でも、中身が空のまま終わっていることがある（9/19 のジョブ2）。
    """
    missing = []
    for rel in job.expected_files:
        path = Path(job.cwd) / rel
        if not path.exists() or (path.is_file() and path.stat().st_size == 0):
            missing.append(rel)
    return missing


def log_tail(job: Job, lines: int = 20) -> str:
    """ジョブのログの末尾。長く running するジョブのログは GB になりうるので、全部は読まない。"""
    path = Path(job.cwd) / "logs" / f"job-{job.id}.log"
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
        tail = f.read()
    # curl などの進捗表示は \r で同じ行を上書きするだけなので、上書きの最終状態だけ残す
    text = tail.decode("utf-8", "replace")
    collapsed = "\n".join(segment.split("\r")[-1] for segment in text.split("\n"))
    return "\n".join(collapsed.splitlines()[-lines:])
