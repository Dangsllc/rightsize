"""The Workbench re-decides routing in the browser from calibration.json. That only works if the
JSON carries every input of the rule: re-deciding from it must match a fresh `calibrate()`."""

import itertools

import pytest
from rightsize_core.calibration.sweep import calibrate, decide

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
N = 200  # resamples; the bootstrap is seeded, so equal N gives equal bounds


@pytest.fixture(scope="module")
def base(states):
    sdir = states[0] / "sweeps" / states[1]["sweep_complete"]
    return sdir, calibrate(ROOT, sdir, {}, 0.03, n_resamples=N)


@pytest.mark.parametrize("tau,delta,floor", list(itertools.product(
    [None, 0.5, 0.8, 0.9], [0.0, 0.03, 0.2], [None, {"recall@5": 0.9}])))
def test_redeciding_from_json_matches_calibrate(base, tau, delta, floor):
    sdir, stored = base
    thresholds = {} if tau is None else {"ndcg@10": tau}
    fresh = calibrate(ROOT, sdir, thresholds, delta, floor, n_resamples=N)
    for old, new in zip(stored["decisions"], fresh["decisions"], strict=True):
        assert (old["task_type"], old["stratum"]) == (new["task_type"], new["stratum"])
        chosen, why = decide(old["rungs"], old["best"], tau, delta, floor)
        assert (chosen, why) == (new["chosen"], new["why"]), old["stratum"]


def test_every_rung_carries_its_paired_difference(base):
    _, stored = base
    for d in stored["decisions"]:
        for name, r in d["rungs"].items():
            assert r["diff_lo"] is not None
            if name == d["best"]:
                assert r["diff_lo"] == 0.0
            assert "ndcg@10" in r["metrics"]


def test_browser_rule_matches_python(base):
    """decide.js (what the Workbench runs in the browser) against decide() (what calibrate runs)."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    _, stored = base
    script = ROOT / "packages/rightsize-ui/src/rightsize_ui/static/decide.js"
    cases = [{"rungs": d["rungs"], "best": d["best"], "tau": tau, "delta": delta, "floors": floor or {}}
             for d in stored["decisions"]
             for tau, delta, floor in itertools.product([None, 0.5, 0.85, 0.9], [0.0, 0.03, 0.2],
                                                        [None, {"recall@5": 0.9}])]
    js = (f"const {{decide}} = require({json.dumps(str(script))});"
          "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
          "process.stdout.write(JSON.stringify(cases.map(c => decide(c.rungs, c.best, c.tau, c.delta, c.floors)[0])));")
    out = subprocess.run([node, "-e", js], input=json.dumps(cases), capture_output=True, text=True, check=True)
    browser = json.loads(out.stdout)
    python = [decide(c["rungs"], c["best"], c["tau"], c["delta"], c["floors"])[0] for c in cases]
    assert browser == python
