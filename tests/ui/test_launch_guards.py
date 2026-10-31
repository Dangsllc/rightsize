"""Launching spends money and can move held-out data, so the plan has to stop the unsafe cases
before a process starts, and a started job has to stop when asked."""

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml
from rightsize_core.service import jobs
from rightsize_core.service.models import PlanRequest
from rightsize_core.service.plans import build_plan

ROOT = Path(__file__).resolve().parents[2]


def plan(req, **kw):
    return asyncio.run(build_plan(ROOT, req, **kw))


def blocking(p) -> dict[str, str]:
    return {g.name: g.message for g in p.guards if g.blocking and not g.ok}


@pytest.fixture()
def fake_ladder(tmp_path):
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(yaml.safe_dump({"as_of": "t", "models": {"fake-small": {"input": 0.1, "output": 0.4},
                                                                 "fake-large": {"input": 10, "output": 40}}}))
    ladder = tmp_path / "ladder.yaml"
    ladder.write_text(yaml.safe_dump({"rungs": [{"name": "small", "model": "fake-small"},
                                                {"name": "large", "model": "fake-large",
                                                 "params": {"no_heldout": True}}]}))
    return str(ladder), str(pricing)


def test_heldout_refused_for_untrusted_system():
    p = plan(PlanRequest(kind="run", system="http://127.0.0.1:9", split="heldout"))
    assert "trusted: true" in blocking(p)["held-out"]
    assert not p.ok


def test_heldout_refused_for_no_heldout_rung(plugin_path, fake_ladder):
    ladder, pricing = fake_ladder
    p = plan(PlanRequest(kind="sweep", system="plugin:fake_ladder_system:make", split="heldout",
                         task_types=["retrieval"], ladder=ladder, max_cost=1), pricing_file=pricing)
    assert "large is marked no_heldout" in blocking(p).get("held-out", "")


def test_priced_models_need_a_cap(plugin_path, fake_ladder):
    ladder, pricing = fake_ladder
    req = PlanRequest(kind="sweep", system="plugin:fake_ladder_system:make", task_types=["retrieval"], ladder=ladder)
    assert "spend cap" in blocking(plan(req, pricing_file=pricing))
    capped = plan(req.model_copy(update={"max_cost": 1.0}), pricing_file=pricing)
    assert capped.ok, blocking(capped)
    assert any("in flight may finish" in g.message for g in capped.guards)


def test_unpriced_model_needs_acknowledgement(plugin_path):
    req = PlanRequest(kind="run", system="plugin:fake_ladder_system:make", model="fake-small",
                      task_types=["retrieval"])
    p = plan(req)
    assert "can't stop it" in blocking(p)["unknown cost"]
    assert plan(req.model_copy(update={"ack_unpriced": True})).ok


def test_missing_key_blocks_and_never_shows_a_value(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    req = PlanRequest(kind="run", system="ref:llm", model="gemini-3.1-flash-lite", max_cost=1,
                      params={"provider": "openai", "base_url": "http://127.0.0.1:9", "api_key_env": "GEMINI_API_KEY"})
    p = plan(req)
    assert "GEMINI_API_KEY is not set" in blocking(p)["keys"]
    monkeypatch.setenv("GEMINI_API_KEY", "sk-secret-value")
    p2 = plan(req)
    assert "keys" not in blocking(p2)
    assert "sk-secret-value" not in p2.model_dump_json()
    assert p2.plan_id != p.plan_id  # the plan changed, so a launch with the old id is stale


def test_plan_id_is_stable_for_the_same_inputs(plugin_path):
    req = PlanRequest(kind="run", system="plugin:fake_ladder_system:make", model="fake-small",
                      task_types=["retrieval"], ack_unpriced=True)
    assert plan(req).plan_id == plan(req).plan_id


def test_blocked_plan_cannot_start(tmp_path):
    p = plan(PlanRequest(kind="run", system="http://127.0.0.1:9", split="heldout"))
    with pytest.raises(jobs.PlanBlocked):
        jobs.start_plan(ROOT, tmp_path, p)


def _wait(runs, job_id, until, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.load(runs, job_id)
        if until(j):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached the expected state: {jobs.load(runs, job_id)}")


def test_cancel_leaves_the_run_aborted_cancelled(plugin_path, tmp_path):
    runs = tmp_path / "runs"
    argv = ["run", "--system", "plugin:scripted_systems:slow", "--task-type", "retrieval", "--concurrency", "1"]
    job = jobs.start(ROOT, runs, argv, "run")
    j = _wait(runs, job.job_id, lambda j: j.run_ids and (runs / j.run_ids[0] / "results.jsonl").exists())
    jobs.cancel(runs, job.job_id)
    j = _wait(runs, job.job_id, lambda j: j.state != "running")
    assert j.state == "cancelled"
    m = json.loads((runs / j.run_ids[0] / "manifest.json").read_text())
    assert m["status"] == "aborted" and "aborted: cancelled" in m["notes"]


def test_circuit_breaker_stops_a_run_that_only_fails(plugin_path, tmp_path):
    runs = tmp_path / "runs"
    argv = ["run", "--system", "plugin:scripted_systems:failing", "--task-type", "retrieval", "--concurrency", "1",
            "--stop-after-errors", "5"]
    job = jobs.start(ROOT, runs, argv, "run")
    j = _wait(runs, job.job_id, lambda j: j.state != "running")
    m = json.loads((runs / j.run_ids[0] / "manifest.json").read_text())
    rows = (runs / j.run_ids[0] / "results.jsonl").read_text().splitlines()
    # Every call that went out errored, so the run is failed (not just stopped early); the
    # breaker's note says why it ended after 5 calls.
    assert m["status"] == "failed: all 5 calls errored" and len(rows) == 5
    assert any("first 5 calls all failed" in n for n in m["notes"])


def test_a_finished_plan_can_run_again(tmp_path):
    """A baseline with no model has the same plan every time; relaunching it must start a new run."""
    runs = tmp_path / "runs"
    p = plan(PlanRequest(kind="run", system="ref:bm25", task_types=["retrieval"], limit=3))
    first = jobs.start_plan(ROOT, runs, p)
    _wait(runs, first.job_id, lambda j: j.state != "running")
    second = jobs.start_plan(ROOT, runs, p)
    assert second.job_id != first.job_id
    again = jobs.start_plan(ROOT, runs, p)  # while the second is still running: a double click
    assert again.job_id == second.job_id
    _wait(runs, second.job_id, lambda j: j.state != "running")


def test_cancelling_a_sweep_records_where_it_stopped(plugin_path, tmp_path):
    from rightsize_core.service.index import Index

    runs = tmp_path / "runs"
    ladder = tmp_path / "ladder.yaml"
    ladder.write_text(yaml.safe_dump({"rungs": [{"name": "one", "model": "m1"}, {"name": "two", "model": "m2"}]}))
    argv = ["sweep", "--system", "plugin:scripted_systems:slow", "--ladder", str(ladder), "--split", "public",
            "--task-type", "retrieval", "--concurrency", "1"]
    job = jobs.start(ROOT, runs, argv, "sweep")
    _wait(runs, job.job_id, lambda j: j.run_ids and (runs / j.run_ids[0] / "results.jsonl").exists())
    ix = Index(ROOT, runs)
    assert ix.sweep(jobs.load(runs, job.job_id).sweep_id).state == "running"
    jobs.cancel(runs, job.job_id)
    j = _wait(runs, job.job_id, lambda j: j.state != "running")
    assert j.state == "cancelled"
    s = ix.sweep(j.sweep_id)
    assert [r.status for r in s.rungs] == ["cancelled", "pending"]
    assert s.state == "cancelled"
    m = json.loads((runs / j.run_ids[0] / "manifest.json").read_text())
    assert m["status"] == "aborted" and "aborted: cancelled" in m["notes"]
    assert ix.summary(j.run_ids[0]).health == "aborted"


def test_plans_reach_mcp_systems(monkeypatch):
    """An MCP connection must open and close in one task; a plan used to break that and report a
    healthy MCP system as failing its preflight."""
    import sys

    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))
    spec = f"mcp+stdio:{sys.executable} -m rightsize_core.cli serve bm25 --transport mcp-stdio"
    p = plan(PlanRequest(kind="run", system=spec, task_types=["retrieval"], limit=2))
    system = next(g for g in p.guards if g.name == "system")
    assert system.ok, system.message
