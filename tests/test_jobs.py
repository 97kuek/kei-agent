import json
import os

import pytest
from fakes import FakePueue, write_request

from ezra import themes
from ezra.jobs import (
    JobManager,
    JobRequestError,
    build_job_command,
    interpret_pueue_status,
    job_env,
    log_tail,
    parse_request,
)


@pytest.fixture
def theme(config):
    ws = themes.resolve(config, "vlm")
    themes.ensure_workspace(ws)
    (ws.cwd / "scripts").mkdir()
    (ws.cwd / "scripts" / "sweep.py").write_text("print('hi')")
    return ws


# build_job_command

def test_command_runs_python_script_with_log(theme):
    cmd = build_job_command(theme.cwd, "scripts/sweep.py", ["--n", "3; rm -rf /"], 7)
    assert cmd == "mkdir -p logs && exec python3 scripts/sweep.py --n '3; rm -rf /' > logs/job-7.log 2>&1"


def test_command_uses_uv_when_theme_has_pyproject(theme):
    (theme.cwd / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert "exec uv run python scripts/sweep.py" in build_job_command(theme.cwd, "scripts/sweep.py", [], 1)


@pytest.mark.parametrize("script", ["/etc/passwd", "../other/x.py", "", "scripts/missing.py"])
def test_command_rejects_paths_outside_or_missing(theme, script):
    with pytest.raises(JobRequestError):
        build_job_command(theme.cwd, script, [], 1)


def test_command_rejects_symlink_escape(theme, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text("")
    os.symlink(outside, theme.cwd / "scripts" / "link.py")
    with pytest.raises(JobRequestError):
        build_job_command(theme.cwd, "scripts/link.py", [], 1)


def test_command_rejects_other_suffix(theme):
    (theme.cwd / "run.rb").write_text("")
    with pytest.raises(JobRequestError):
        build_job_command(theme.cwd, "run.rb", [], 1)


def test_job_env_drops_secrets_and_ezras_own_venv(config):
    """ジョブは研究の環境で動かす。uv run が足す Ezra の .venv/bin を持ち込まない。"""
    venv = str(config.repo_root / ".venv" / "bin")
    env = job_env(
        {"PATH": f"{venv}:/bin", "HOME": "/h", "VIRTUAL_ENV": venv,
         "SLACK_BOT_TOKEN": "x", "EZRA_ALLOWED_USER_ID": "U"},
        config.repo_root,
    )
    assert env == {"PATH": "/bin", "HOME": "/h"}


def test_parse_request_rejects_a_cancel_without_a_number():
    """job_id が数字でない依頼でも、ジョブの仕組みごと壊れないようにする。"""
    for bad in (None, "abc", [1], {}):
        with pytest.raises(JobRequestError):
            parse_request({"action": "cancel", "request_id": "r", "job_id": bad})
    with pytest.raises(JobRequestError):
        parse_request({"action": "cancel", "request_id": "r"})


def test_parse_request_validates_args():
    with pytest.raises(JobRequestError):
        parse_request({"action": "submit", "request_id": "r", "args": "not-a-list"})
    with pytest.raises(JobRequestError):
        parse_request({"action": "delete", "request_id": "r"})


# pueue の状態

@pytest.mark.parametrize("status, expected", [
    ({"Queued": {"enqueued_at": "2026-09-17T10:00:00+09:00"}}, "queued"),
    ({"Running": {"enqueued_at": "2026-09-17T10:00:00+09:00", "start": "2026-09-17T10:00:01+09:00"}}, "running"),
    ({"Done": {"start": "2026-09-17T10:00:01+09:00", "end": "2026-09-17T10:05:01+09:00", "result": "Success"}}, "succeeded"),
    ({"Done": {"start": "2026-09-17T10:00:01+09:00", "end": "2026-09-17T10:00:02+09:00", "result": {"Failed": 3}}}, "failed"),
    ({"Done": {"start": None, "end": "2026-09-17T10:00:02+09:00", "result": "Killed"}}, "cancelled"),
    ("Running", "running"),
])
def test_interpret_pueue_status(status, expected):
    assert interpret_pueue_status({"status": status})[0] == expected


# JobManager

async def test_submit_request_from_known_thread(config, store, theme):
    store.upsert_thread("C1", "100.1", "vlm", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    path = write_request(theme.cwd, action="submit", request_id="r1", channel="C1", thread_ts="100.1",
                         name="sweep", script="scripts/sweep.py", args=["--n", "3"])

    handled = await manager.process_requests(theme.cwd)

    assert not path.exists()
    outcome, = handled
    job, error = outcome.job, outcome.error
    assert error is None and job.pueue_id == 0 and job.status == "queued"
    assert pueue.added[0][2] == f"ezra-{job.id}"
    state = json.loads((theme.cwd / ".ezra" / "jobs" / f"{job.id}.json").read_text())
    assert state["status"] == "queued" and state["log"] == f"logs/job-{job.id}.log"

    # 同じ依頼をもう一度置いても二重に投入しない
    write_request(theme.cwd, action="submit", request_id="r1", channel="C1", thread_ts="100.1",
                  name="sweep", script="scripts/sweep.py", args=[])
    assert await manager.process_requests(theme.cwd) == []
    assert len(pueue.added) == 1


async def test_submit_rejects_thread_of_another_theme(config, store, theme):
    store.upsert_thread("C2", "200.1", "other", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    write_request(theme.cwd, action="submit", request_id="r2", channel="C2", thread_ts="200.1",
                  name="x", script="scripts/sweep.py", args=[])

    outcome, = await manager.process_requests(theme.cwd)

    assert outcome.error and outcome.job.status == "rejected" and pueue.added == []


async def test_refresh_reports_finished_job_once(config, store, theme):
    store.upsert_thread("C1", "100.1", "vlm", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    write_request(theme.cwd, action="submit", request_id="r3", channel="C1", thread_ts="100.1",
                  name="sweep", script="scripts/sweep.py", args=[])
    job = (await manager.process_requests(theme.cwd))[0].job

    assert await manager.refresh() == []

    pueue.task_status[job.pueue_id] = {"status": {"Done": {
        "start": "2026-09-17T10:00:00+09:00", "end": "2026-09-17T10:05:00+09:00", "result": "Success"}}}
    finished, = await manager.refresh()
    assert finished.status == "succeeded" and finished.finished_at - finished.started_at == 300
    assert pueue.removed == [job.pueue_id]

    manager.mark_reported(finished)
    assert await manager.refresh() == []


async def test_cancel_request_kills_running_job(config, store, theme):
    store.upsert_thread("C1", "100.1", "vlm", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    write_request(theme.cwd, action="submit", request_id="r4", channel="C1", thread_ts="100.1",
                  name="sweep", script="scripts/sweep.py", args=[])
    job = (await manager.process_requests(theme.cwd))[0].job

    write_request(theme.cwd, action="cancel", request_id="c1", job_id=job.id)
    await manager.process_requests(theme.cwd)

    assert pueue.killed == [job.pueue_id]


async def test_unreadable_request_is_reported(config, store, theme):
    """読めない依頼を黙って捨てると、Claude は「投入した」と思ったまま待ち続ける。"""
    manager = JobManager(config, store, FakePueue())
    path = theme.cwd / ".ezra" / "requests" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"action": "cancel", "request_id": "c1", "job_id": null, "channel": "C1", "thread_ts": "1.1"}')

    outcome, = await manager.process_requests(theme.cwd)

    assert outcome.error and "読めませんでした" in outcome.error
    assert (outcome.channel, outcome.thread_ts) == ("C1", "1.1")
    assert not path.exists()  # 残すと毎分同じ失敗を繰り返す


# log_tail

def test_log_tail_collapses_carriage_return_progress_lines(store, theme):
    """curl の進捗表示は \r で同じ行を上書きするだけなので、末尾には最終状態の1行だけ残したい。"""
    job = store.add_job("r5", "C1", "100.1", str(theme.cwd), "sweep", "", status="queued")
    log_dir = theme.cwd / "logs"
    log_dir.mkdir(exist_ok=True)
    progress = "\r".join(f"{p}%" for p in range(0, 101, 10))
    (log_dir / f"job-{job.id}.log").write_text(
        f"start\n{progress}\ndone downloading\nTraceback (most recent call last):\nValueError: boom\n"
    )

    tail = log_tail(job, lines=10)

    assert tail.splitlines() == [
        "start",
        "100%",
        "done downloading",
        "Traceback (most recent call last):",
        "ValueError: boom",
    ]


async def test_job_being_submitted_is_not_marked_failed(config, store, theme):
    """pueue に投入し終える前に状態を見に行っても、動いているジョブを失敗と決めつけない。"""
    store.upsert_thread("C1", "100.1", "vlm", None)
    manager = JobManager(config, store, FakePueue())
    job = store.add_job("r9", "C1", "100.1", str(theme.cwd), "sweep", "", status="queued")
    assert job.pueue_id is None

    assert await manager.refresh() == []
    assert store.get_job(job.id).status == "queued"
