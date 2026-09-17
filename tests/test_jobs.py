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
    parse_request,
)


@pytest.fixture
def theme(config):
    ws = themes.resolve(config, "theme-vlm")
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


def test_job_env_drops_secrets():
    env = job_env({"PATH": "/bin", "HOME": "/h", "SLACK_BOT_TOKEN": "x", "EZRA_ALLOWED_USER_ID": "U"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


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
    store.upsert_thread("C1", "100.1", "theme-vlm", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    path = write_request(theme.cwd, action="submit", request_id="r1", channel="C1", thread_ts="100.1",
                         name="sweep", script="scripts/sweep.py", args=["--n", "3"])

    handled = await manager.process_requests(theme.cwd)

    assert not path.exists()
    (job, error), = handled
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
    store.upsert_thread("C2", "200.1", "theme-other", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    write_request(theme.cwd, action="submit", request_id="r2", channel="C2", thread_ts="200.1",
                  name="x", script="scripts/sweep.py", args=[])

    (job, error), = await manager.process_requests(theme.cwd)

    assert error and job.status == "rejected" and pueue.added == []


async def test_refresh_reports_finished_job_once(config, store, theme):
    store.upsert_thread("C1", "100.1", "theme-vlm", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    write_request(theme.cwd, action="submit", request_id="r3", channel="C1", thread_ts="100.1",
                  name="sweep", script="scripts/sweep.py", args=[])
    (job, _), = await manager.process_requests(theme.cwd)

    assert await manager.refresh() == []

    pueue.task_status[job.pueue_id] = {"status": {"Done": {
        "start": "2026-09-17T10:00:00+09:00", "end": "2026-09-17T10:05:00+09:00", "result": "Success"}}}
    finished, = await manager.refresh()
    assert finished.status == "succeeded" and finished.finished_at - finished.started_at == 300
    assert pueue.removed == [job.pueue_id]

    manager.mark_reported(finished)
    assert await manager.refresh() == []


async def test_cancel_request_kills_running_job(config, store, theme):
    store.upsert_thread("C1", "100.1", "theme-vlm", None)
    pueue = FakePueue()
    manager = JobManager(config, store, pueue)
    write_request(theme.cwd, action="submit", request_id="r4", channel="C1", thread_ts="100.1",
                  name="sweep", script="scripts/sweep.py", args=[])
    (job, _), = await manager.process_requests(theme.cwd)

    write_request(theme.cwd, action="cancel", request_id="c1", job_id=job.id)
    await manager.process_requests(theme.cwd)

    assert pueue.killed == [job.pueue_id]
