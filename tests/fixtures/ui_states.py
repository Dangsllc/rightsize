"""Build a runs directory holding every state the Workbench has to show, from fake systems.

    uv run python tests/fixtures/ui_states.py <runs_dir>

The same directory backs the Workbench tests and design review (`rightsize ui --runs-dir <dir>`).
Nothing here calls a real model.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent

PRICES = {"fake-small": {"input": 0.10, "output": 0.40}, "fake-medium": {"input": 1.0, "output": 4.0},
          "fake-large": {"input": 10.0, "output": 40.0}}


def _cli(args: list[str]) -> str:
    from rightsize_core.cli import app
    from typer.testing import CliRunner

    res = CliRunner().invoke(app, args)
    if res.exit_code != 0:
        raise RuntimeError(f"rightsize {' '.join(args)} failed:\n{res.output}\n{res.exception!r}")
    return res.output


def _run_dir(output: str) -> str:
    return Path(next(ln for ln in output.splitlines() if ln.strip().endswith("scorecard.html")).strip()).parent.name


def _sweep_id(output: str) -> str:
    return Path(next(ln for ln in output.splitlines() if ln.startswith("sweep_dir=")).split("=", 1)[1]).name


ENV = ("RIGHTSIZE_ROOT", "RIGHTSIZE_RUNS_DIR", "RIGHTSIZE_PLUGIN_PATH")


def build(runs_dir: Path) -> dict[str, str]:
    """Returns {state: run or sweep id}. Leaves the process environment as it found it."""
    saved = {k: os.environ.get(k) for k in ENV}
    os.environ.update(RIGHTSIZE_ROOT=str(ROOT), RIGHTSIZE_RUNS_DIR=str(runs_dir), RIGHTSIZE_PLUGIN_PATH=str(FIXTURES))
    try:
        return _build(runs_dir)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _build(runs_dir: Path) -> dict[str, str]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    work = runs_dir.parent / f"{runs_dir.name}-config"
    work.mkdir(exist_ok=True)
    pricing = work / "pricing.yaml"
    pricing.write_text(yaml.safe_dump({"as_of": "fixture", "models": PRICES}))
    ladder = work / "ladder.yaml"
    ladder.write_text(yaml.safe_dump({"rungs": [{"name": n.removeprefix("fake-"), "model": n} for n in PRICES]}))
    base = ["--suite", "m1", "--split", "public", "--task-type", "retrieval", "--pricing", str(pricing)]
    ids: dict[str, str] = {}

    out = _cli(["sweep", "--system", "plugin:fake_ladder_system:make", "--ladder", str(ladder), *base])
    ids["sweep_complete"] = _sweep_id(out)
    sdir = runs_dir / "sweeps" / ids["sweep_complete"]
    _cli(["calibrate", str(sdir), "--threshold", "ndcg@10=0.85", "--emit", str(sdir / "routing.yaml")])
    record = json.loads((sdir / "sweep.json").read_text())
    ids["run_ok"] = Path(record["runs"][-1]["run_dir"]).name
    ids["run_ok_small"] = Path(record["runs"][0]["run_dir"]).name

    out = _cli(["sweep", "--system", "plugin:fake_ladder_system:make", "--ladder", str(ladder),
                "--rungs", "small,large", "--sample", "6", *base])
    ids["sweep_low_n"] = _sweep_id(out)
    _cli(["calibrate", str(runs_dir / "sweeps" / ids["sweep_low_n"]), "--threshold", "ndcg@10=0.85"])

    ids["run_unknown_cost"] = _run_dir(_cli(["run", "--system", "plugin:fake_ladder_system:make", "--model",
                                             "fake-small", "--param", "hide_usage=true", *base]))
    ids["run_failed"] = _run_dir(_cli(["run", "--system", "plugin:scripted_systems:failing", *base]))
    # The same all-error run as an older Rightsize recorded it (status ok, no error_rate): it scores
    # nothing, so it reads as no_signal.
    src = runs_dir / ids["run_failed"]
    legacy = runs_dir / src.name.replace("scripted", "scripted-legacy")
    legacy.mkdir(exist_ok=True)
    m = json.loads((src / "manifest.json").read_text())
    m.update(run_id=legacy.name, status="ok", notes=[])
    m.pop("error_rate", None)
    (legacy / "manifest.json").write_text(json.dumps(m, indent=2))
    (legacy / "results.jsonl").write_text((src / "results.jsonl").read_text())
    card = json.loads((src / "scorecard.json").read_text())
    card.update(run_id=legacy.name, status="ok")
    (legacy / "scorecard.json").write_text(json.dumps(card, indent=2))
    ids["run_no_signal"] = legacy.name
    ids["run_degraded"] = _run_dir(_cli(["run", "--system", "plugin:scripted_systems:flaky", *base]))
    ids["run_aborted"] = _run_dir(_cli(["run", "--system", "plugin:fake_ladder_system:make", "--model", "fake-large",
                                        "--max-cost", "0.05", "--concurrency", "1", *base]))

    # A classification run (the oracle answers from the labels, so no model is called) for item views
    # with whole documents.
    ids["run_classification"] = _run_dir(_cli(["run", "--system", "ref:oracle", "--suite", "m2", "--split",
                                               "public", "--task-type", "control_classification", "--limit", "2"]))

    # A run whose process died: the manifest still says running and there is no heartbeat.
    src = runs_dir / ids["run_ok_small"]
    dead = runs_dir / src.name.replace("fake-small", "fake-medium-killed")
    dead.mkdir(exist_ok=True)
    m = json.loads((src / "manifest.json").read_text())
    m.update(run_id=dead.name, status="running", ended_at=None, sweep_id=None)
    (dead / "manifest.json").write_text(json.dumps(m, indent=2))
    rows = (src / "results.jsonl").read_text().splitlines()
    (dead / "results.jsonl").write_text("\n".join(rows[: len(rows) // 3]) + "\n")
    ids["run_interrupted"] = dead.name

    # A sweep directory from before sweeps recorded anything up front.
    empty = runs_dir / "sweeps" / "20261004T031002Z_8014ea"
    empty.mkdir(parents=True, exist_ok=True)
    ids["sweep_empty"] = empty.name
    (runs_dir.parent / f"{runs_dir.name}-ids.json").write_text(json.dumps(ids, indent=2))
    return ids


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1]).resolve()), indent=2))
