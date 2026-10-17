"""Launch runs, sweeps, and calibrations as background processes that outlive whoever started them.

A job is a directory under runs/jobs/<job_id>/:
  job.json     what was launched, by whom (ui | mcp | cli), from which plan
  log.txt      the CLI's output
  dirs.jsonl   run and sweep directories the CLI created (it appends as it goes)
  exit.json    exit code, written by the job runner when the CLI exits
  cancel.json  present if someone asked the job to stop

The job runner (`python -m rightsize_core.service.jobrunner <job_dir>`) leads its own process
group, so cancelling sends SIGINT to the group and the CLI records `aborted: cancelled` in each
manifest before exiting.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rightsize_core.service.index import NotFound, check_id, pid_alive
from rightsize_core.service.models import Job, Plan


class PlanBlocked(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def jobs_dir(runs_dir: Path) -> Path:
    return runs_dir / "jobs"


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load(runs_dir: Path, job_id: str) -> Job:
    d = jobs_dir(runs_dir) / check_id(job_id)
    if not (d / "job.json").exists():
        raise NotFound(job_id)
    job = Job.model_validate_json((d / "job.json").read_text())
    if (d / "dirs.jsonl").exists():
        for line in (d / "dirs.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            name = Path(rec["path"]).name
            if rec["kind"] == "run" and name not in job.run_ids:
                job.run_ids.append(name)
            elif rec["kind"] == "sweep":
                job.sweep_id = name
    cancelled = (d / "cancel.json").exists()
    if (d / "exit.json").exists():
        ex = json.loads((d / "exit.json").read_text())
        job.exit_code, job.ended_at = ex.get("code"), ex.get("ended_at")
        job.state = "cancelled" if cancelled else "finished" if job.exit_code == 0 else "failed"
    elif pid_alive(job.pid):
        job.state = "running"
    else:
        job.state = "cancelled" if cancelled else "lost"
    if (d / "log.txt").exists():
        job.log_tail = (d / "log.txt").read_text(errors="replace").splitlines()[-40:]
    return job


def list_jobs(runs_dir: Path) -> list[Job]:
    d = jobs_dir(runs_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir(), reverse=True):
        try:
            out.append(load(runs_dir, p.name))
        except (NotFound, ValueError, OSError):
            continue
    return out


def find_by_plan(runs_dir: Path, plan_id: str) -> Job | None:
    return next((j for j in list_jobs(runs_dir) if j.plan_id == plan_id and j.state == "running"), None)


def start(root: Path, runs_dir: Path, argv: list[str], kind: str, launched_by: str = "ui",
          plan_id: str | None = None) -> Job:
    job_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{kind}_{uuid.uuid4().hex[:6]}"
    d = jobs_dir(runs_dir) / job_id
    d.mkdir(parents=True)
    job = Job(job_id=job_id, kind=kind, argv=argv, launched_by=launched_by, plan_id=plan_id, created_at=_now())
    _write(d / "job.json", job.model_dump(mode="json"))
    env = {**os.environ, "RIGHTSIZE_ROOT": str(root), "RIGHTSIZE_RUNS_DIR": str(runs_dir),
           "RIGHTSIZE_JOB_DIR": str(d), "PYTHONUNBUFFERED": "1"}
    with (d / "log.txt").open("w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "rightsize_core.service.jobrunner", str(d)],
                                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    job.pid = proc.pid
    _write(d / "job.json", job.model_dump(mode="json", exclude={"run_ids", "sweep_id", "log_tail"}))
    return job


def start_plan(root: Path, runs_dir: Path, plan: Plan, launched_by: str = "ui") -> Job:
    """Launch a plan whose guards pass. Launching a plan whose job is still running returns that job
    (a double click or an agent's retry), but a finished plan can always run again."""
    if not plan.ok:
        bad = [g.message for g in plan.guards if g.blocking and not g.ok]
        raise PlanBlocked("; ".join(bad))
    existing = find_by_plan(runs_dir, plan.plan_id)
    if existing is not None and existing.state == "running":
        return existing
    return start(root, runs_dir, plan.argv, plan.request.kind, launched_by, plan.plan_id)


def cancel(runs_dir: Path, job_id: str) -> Job:
    job = load(runs_dir, job_id)
    d = jobs_dir(runs_dir) / job_id
    if job.state != "running":
        return job
    _write(d / "cancel.json", {"at": _now()})
    try:
        os.killpg(os.getpgid(job.pid), signal.SIGINT)
    except ProcessLookupError:
        pass
    return load(runs_dir, job_id)
