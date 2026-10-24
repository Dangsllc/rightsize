"""Every state a run or sweep can be left in maps to the health the Workbench shows for it."""

import json
import os
import time

import pytest
from rightsize_core.runner.run import HEARTBEAT_FILE

EXPECTED = {
    "run_ok": "ok",
    "run_unknown_cost": "ok",
    "run_failed": "failed",
    "run_no_signal": "no_signal",
    "run_degraded": "degraded",
    "run_aborted": "aborted",
    "run_interrupted": "interrupted",
}


@pytest.mark.parametrize("state", sorted(EXPECTED))
def test_run_health(index, states, state):
    assert index.summary(states[1][state]).health == EXPECTED[state]


@pytest.mark.parametrize("state", ["run_failed", "run_no_signal"])
def test_all_error_runs_name_the_cause_and_a_fix(index, states, state):
    s = index.summary(states[1][state])
    assert "401 Unauthorized" in s.health_reason
    assert s.error_groups and s.error_groups[0].count == s.progress.done
    assert "env var" in (s.error_groups[0].hint or "")
    assert not any(h.value is not None for h in s.headlines)


def test_unknown_cost_is_not_zero(index, states):
    unknown = index.summary(states[1]["run_unknown_cost"]).headlines[0]
    known = index.summary(states[1]["run_ok"]).headlines[0]
    assert (unknown.cost_state, unknown.cost_per_task) == ("unknown", None)
    assert known.cost_state == "known" and known.cost_per_task > 0
    assert any("unknown" in c and "not $0" in c for c in index.summary(states[1]["run_unknown_cost"]).caveats)


def test_capped_run_stopped_early(index, states):
    s = index.summary(states[1]["run_aborted"])
    # $0.016 a call under a $0.05 cap, one call at a time: the 4th call crosses the cap, and nothing follows.
    assert s.progress.done == int(0.05 / 0.016) + 1
    h = s.headlines[0]
    assert h.tiny_n and h.low_n
    assert any("too few to estimate uncertainty" in c for c in s.caveats)


def test_fresh_heartbeat_is_running_and_stale_is_interrupted(index, states):
    d = index.runs_dir / states[1]["run_interrupted"]
    beat = d / HEARTBEAT_FILE
    try:
        beat.write_text("now")
        assert index.summary(d.name).health == "running"
        old = time.time() - 120
        os.utime(beat, (old, old))
        assert index.summary(d.name).health == "interrupted"
    finally:
        beat.unlink(missing_ok=True)


@pytest.mark.parametrize("state,expected", [("sweep_complete", "complete"), ("sweep_low_n", "complete"),
                                            ("sweep_empty", "empty")])
def test_sweep_state(index, states, state, expected):
    assert index.sweep(states[1][state]).state == expected


def test_sweep_rungs_and_spend(index, states):
    s = index.sweep(states[1]["sweep_complete"])
    assert [r.status for r in s.rungs] == ["done", "done", "done"]
    assert all(r.health == "ok" for r in s.rungs)
    assert s.spend_usd > 0 and s.calibrated and s.routing


def test_low_n_sweep_flags_its_decisions(index, states):
    assert index.sweep(states[1]["sweep_low_n"]).low_n_decisions > 0


def test_ids_cannot_escape_runs_dir(index):
    from rightsize_core.service.index import NotFound

    for bad in ["../x", "..", "sweeps", "a/b", ""]:
        with pytest.raises(NotFound):
            index.run_dir(bad)


def test_old_absolute_run_dirs_still_resolve(index, states):
    d = index.sweep_dir(states[1]["sweep_complete"])
    rec = json.loads((d / "sweep.json").read_text())
    moved = dict(rec, runs=[dict(r, run_dir=f"/nowhere/runs/{r['run_dir'].rsplit('/', 1)[-1]}") for r in rec["runs"]])
    for r in moved["rungs"]:
        r.pop("run_dir", None)
    (d / "sweep.json").write_text(json.dumps(moved))
    try:
        assert all(r.run_id and r.health == "ok" for r in index.sweep(d.name).rungs)
    finally:
        (d / "sweep.json").write_text(json.dumps(rec))


def test_lists_never_score_but_opening_a_run_does(index, states, tmp_path):
    import shutil

    from rightsize_core.service import queries
    from rightsize_core.service.index import Index

    runs = tmp_path / "runs"
    src = index.runs_dir / states[1]["run_ok"]
    shutil.copytree(src, runs / src.name)
    (runs / src.name / "scorecard.json").unlink()
    ix = Index(index.root, runs)
    s = ix.summary(src.name)
    assert s.health == "unscored" and s.health_reason.startswith("Being scored")  # it just ended
    assert not (runs / src.name / "scorecard.json").exists()
    m = json.loads((runs / src.name / "manifest.json").read_text())
    m["ended_at"] = "2026-01-01T00:00:00Z"
    (runs / src.name / "manifest.json").write_text(json.dumps(m))
    assert ix.summary(src.name).health_reason.startswith("Not scored yet")
    detail = queries.run_detail(ix, src.name)
    assert detail["summary"].health == "ok" and (runs / src.name / "scorecard.json").exists()


def test_failed_says_every_call_errored(index, states):
    s = index.summary(states[1]["run_failed"])
    assert s.status.startswith("failed: all ") and s.health_reason.startswith("All ")
    assert "calls errored" in s.health_reason
