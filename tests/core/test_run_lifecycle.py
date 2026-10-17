"""A run's manifest must say how it ended: finished, stopped by a guard, cancelled, or crashed."""

import asyncio
import json
from pathlib import Path

import pytest
from rightsize_core.integrity.dataset import load_tasks
from rightsize_core.runner.run import HEARTBEAT_FILE, execute
from rightsize_core.schema import Outcome, OutcomeError, SystemInfo
from rightsize_core.systems.plugin import PluginSystem

ROOT = Path(__file__).resolve().parents[2]


class Scripted:
    """Answers retrieval with a fixed hit, fails, or waits, depending on how it was built."""

    def __init__(self, fail: bool = False, delay: float = 0.0, crash_on_setup: bool = False) -> None:
        self.fail, self.delay, self.crash_on_setup = fail, delay, crash_on_setup
        self.calls = 0

    def info(self):
        return SystemInfo(name="scripted", task_types=["retrieval"])

    async def run(self, task_type, inputs, model, params):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            return Outcome(error=OutcomeError(kind="error", message="401 Unauthorized"))
        return Outcome(prediction={"hits": [{"citation": "160.103"}]})


class CrashingSetup(PluginSystem):
    async def setup(self) -> None:
        raise RuntimeError("could not reach the system")


def _tasks():
    return load_tasks(ROOT, "m1", "public", ["retrieval"])


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text())


async def test_finished_run_is_ok_and_records_pid(tmp_path):
    run_dir = await execute(ROOT, PluginSystem(Scripted()), _tasks()[:5], "m1", "public", runs_dir=tmp_path)
    m = _manifest(run_dir)
    assert m["status"] == "ok"
    assert isinstance(m["host"]["pid"], int)
    assert not (run_dir / HEARTBEAT_FILE).exists()


async def test_circuit_breaker_stops_a_run_that_only_errors(tmp_path):
    impl = Scripted(fail=True)
    run_dir = await execute(ROOT, PluginSystem(impl), _tasks(), "m1", "public", concurrency=2,
                            runs_dir=tmp_path, stop_after_errors=5)
    m = _manifest(run_dir)
    assert m["status"] == "aborted"
    assert any("first 5 calls all failed" in n and "401" in n for n in m["notes"])
    assert impl.calls <= 5 + 2


async def test_breaker_disarms_after_a_success(tmp_path):
    run_dir = await execute(ROOT, PluginSystem(Scripted()), _tasks()[:8], "m1", "public",
                            runs_dir=tmp_path, stop_after_errors=1)
    assert _manifest(run_dir)["status"] == "ok"


async def test_cancelled_run_says_so_and_heartbeats_while_live(tmp_path):
    runs = tmp_path / "runs"
    job = asyncio.create_task(execute(ROOT, PluginSystem(Scripted(delay=0.05)), _tasks(), "m1", "public",
                                      concurrency=1, runs_dir=runs))
    for _ in range(100):
        await asyncio.sleep(0.01)
        dirs = list(runs.glob("*"))
        if dirs and (dirs[0] / HEARTBEAT_FILE).exists():
            break
    run_dir = next(runs.glob("*"))
    assert (run_dir / HEARTBEAT_FILE).exists()
    assert _manifest(run_dir)["status"] == "running"
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    m = _manifest(run_dir)
    assert m["status"] == "aborted"
    assert "aborted: cancelled" in m["notes"]
    assert not (run_dir / HEARTBEAT_FILE).exists()


async def test_crash_is_recorded_not_left_running(tmp_path):
    with pytest.raises(RuntimeError):
        await execute(ROOT, CrashingSetup(Scripted()), _tasks()[:3], "m1", "public", runs_dir=tmp_path)
    m = _manifest(next(tmp_path.glob("*")))
    assert m["status"] == "aborted"
    assert any("could not reach the system" in n for n in m["notes"])


async def test_refusals_disarm_the_breaker(tmp_path):
    class Refuses(Scripted):
        async def run(self, task_type, inputs, model, params):
            self.calls += 1
            return Outcome(error=OutcomeError(kind="refusal", message="I can't help with that"))

    run_dir = await execute(ROOT, PluginSystem(Refuses()), _tasks()[:8], "m1", "public", concurrency=1,
                            runs_dir=tmp_path, stop_after_errors=3)
    assert _manifest(run_dir)["status"] == "ok"
